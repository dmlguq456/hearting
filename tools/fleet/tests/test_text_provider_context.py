"""Background calls honor their profile without inheriting agent bootstraps."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet import refresh_title as rt


class TextProviderContextTest(unittest.TestCase):
    def test_profile_budget_reaches_every_provider_in_one_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            for adapter, flag in (("claude", "--effort"), ("codex", "-c"), ("opencode", "--variant")):
                with self.subTest(adapter=adapter), mock.patch.object(
                    rt, "provider_settings", return_value={"model": "test-model", "budget": "low"}
                ) as resolve, mock.patch.object(rt.opencode_runtime, "is_v2", return_value=False):
                    command = rt.provider_command(adapter, "prompt", workdir=Path(tmp), profile="light")
                    resolve.assert_called_once_with(adapter, mock.ANY, profile="light")
                    argv = command[0]
                    if adapter == "codex":
                        self.assertIn('model_reasoning_effort="low"', argv)
                    else:
                        self.assertEqual(argv[argv.index(flag) + 1], "low")
            self.assertEqual(rt.WORKING_DEBOUNCE_SEC, 300)

    def test_codex_uses_auth_only_home_and_neutral_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "user-codex"
            source.mkdir()
            (source / "auth.json").write_text("{}")
            (source / "AGENTS.md").write_text("GLOBAL BOOTSTRAP")
            (source / "config.toml").write_text('model_reasoning_effort="xhigh"')
            state = Path(tmp) / "state"
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(source)}), mock.patch.object(
                rt, "provider_settings", return_value={"model": "test-model", "budget": "low"}
            ):
                command = rt.provider_command("codex", "prompt", workdir=state)
            runtime = Path(command.env["CODEX_HOME"])
            self.assertEqual((runtime / "auth.json").resolve(), source / "auth.json")
            self.assertFalse((runtime / "AGENTS.md").exists())
            self.assertFalse((runtime / "config.toml").exists())
            self.assertEqual(command.cwd, str(state))
            self.assertEqual((source / "config.toml").read_text(), 'model_reasoning_effort="xhigh"')
            for feature in ("shell_tool", "unified_exec", "multi_agent", "apps", "plugins", "hooks"):
                self.assertIn("features.%s=false" % feature, command[0])

    def test_opencode_keeps_transport_without_inheriting_mcp_and_instructions(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "user-config" / "opencode"
            source.mkdir(parents=True)
            original = ('{ // transport comment\n'
                        '"provider":{"custom":{"options":{"baseURL":"https://host/path/*ok*/"}}},\n'
                        '"mcp":{"foreign":{"command":["foreign"]}},'
                        '"instructions":["GLOBAL.md"],"plugin":["foreign"],}')
            (source / "opencode.jsonc").write_text(original)
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(source.parent)}), mock.patch.object(
                rt, "provider_settings", return_value={"model": "custom/model", "budget": "runtime-default"}
            ), mock.patch.object(rt.opencode_runtime, "is_v2", return_value=False):
                command = rt.provider_command("opencode", "prompt", workdir=Path(tmp) / "state")
            config = json.loads((Path(command.env["XDG_CONFIG_HOME"]) / "opencode/opencode.json").read_text())
            self.assertEqual(config["provider"]["custom"]["options"]["baseURL"], "https://host/path/*ok*/")
            self.assertEqual(config["mcp"], {})
            self.assertEqual(config["instructions"], [])
            self.assertNotIn("plugin", config)
            self.assertIn("runtime-default", command[0])
            self.assertEqual(command[0][command[0].index("--title") + 1], "fleet-titler")
            self.assertEqual(command[0][command[0].index("--log-level") + 1], "WARN")
            self.assertEqual(command[1], "prompt")
            self.assertEqual((source / "opencode.jsonc").read_text(), original)

    def test_opencode_v2_uses_json_cwd_and_v2_no_tools_agent(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
                rt, "provider_settings",
                return_value={"model": "opencode-go/model", "budget": "high"}), \
             mock.patch.object(rt.opencode_runtime, "is_v2", return_value=True), \
             mock.patch.object(rt.opencode_runtime, "select_automatic_model",
                              return_value=("opencode-go/model", "high", {"state": "unknown"})):
            workdir = Path(tmp) / "state"
            command = rt.provider_command("opencode", "prompt", workdir=workdir, profile="light")
            argv = command[0]
            self.assertNotIn("--pure", argv)
            self.assertNotIn("--dir", argv)
            self.assertNotIn("--variant", argv)
            self.assertNotIn("--log-level", argv)
            self.assertEqual(argv[argv.index("--format") + 1], "json")
            self.assertEqual(argv[argv.index("--model") + 1], "opencode-go/model#high")
            self.assertEqual(argv[argv.index("--agent") + 1], "hearting-fleet-titler")
            self.assertTrue(argv[argv.index("--title") + 1].startswith("hearting-worker-"))
            agent = workdir / ".opencode" / "agents" / "hearting-fleet-titler.md"
            self.assertTrue(agent.is_file())
            self.assertIn("action: execute", agent.read_text())
            self.assertIn("effect: deny", agent.read_text())
            config = json.loads((Path(command.env["XDG_CONFIG_HOME"])
                                 / "opencode/opencode.json").read_text())
            self.assertIn("permissions", config)
            self.assertEqual(config["mcp"], {"servers": {}})
            self.assertEqual(config["plugins"], [])
            project_config = json.loads((workdir / ".opencode/opencode.json").read_text())
            self.assertEqual(project_config["policies"][0],
                             {"action": "tool.use", "resource": "*", "effect": "deny"})
            self.assertEqual(project_config["policies"][-1]["effect"], "allow")
            with mock.patch.object(rt.opencode_runtime, "record_model_outcome") as record, \
                 mock.patch.object(rt.subprocess, "run", return_value=mock.Mock(
                     returncode=0, stdout="\n".join((
                         json.dumps({"type": "step_start", "sessionID": "ses_x"}),
                         json.dumps({"type": "text", "sessionID": "ses_x",
                                     "part": {"text": "TITLE: A\nNOW: B"}}), "")))):
                self.assertEqual(rt.run_provider_cascade([command], timeout=1, env={}),
                                 ("TITLE: A\nNOW: B", 0))
            record.assert_called_once()

    def test_opencode_v2_unverified_model_runs_only_for_an_explicit_provider(self):
        unverified = ("openai/gpt-6.1-sol", "xhigh",
                      {"state": "unknown", "reason": "provider-model-unverified",
                       "automatic_eligible": False})
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
                rt, "provider_settings",
                return_value={"model": "openai/gpt-6.1-sol", "budget": "xhigh"}), \
             mock.patch.object(rt.opencode_runtime, "is_v2", return_value=True), \
             mock.patch.object(rt.opencode_runtime, "select_automatic_model",
                               return_value=unverified):
            workdir = Path(tmp) / "state"
            self.assertIsNone(rt.provider_command("opencode", "prompt", workdir=workdir,
                                                  profile="deep"))
            command = rt.provider_command("opencode", "prompt", workdir=workdir,
                                          profile="deep", explicit_provider=True)
            self.assertEqual(command[0][command[0].index("--model") + 1],
                             "openai/gpt-6.1-sol#xhigh")

    def test_title_pin_is_the_only_explicit_provider(self):
        env = {key: value for key, value in os.environ.items()
               if key not in ("FLEET_TITLE_COMMAND", "FLEET_TITLE_MODEL")}
        for pinned, expected in (("opencode", True), ("", False)):
            with self.subTest(pinned=pinned), \
                 mock.patch.dict(os.environ, {**env, "FLEET_TITLE_PROVIDER": pinned}, clear=True), \
                 mock.patch.object(rt, "selected_providers", return_value=("opencode",)), \
                 mock.patch.object(rt, "provider_command", return_value=None) as command:
                rt._resolve_commands("prompt")
                self.assertEqual(command.call_args.kwargs["explicit_provider"], expected)

    def test_cached_and_ranked_orders_read_the_shared_automatic_rule(self):
        cases = (([{"model": "openai/gpt-6.1-sol", "state": "unknown",
                    "reason": "provider-model-unverified"}], False),
                 ([{"model": "openai/gpt-6.1-sol", "state": "unknown"},
                   {"model": "opencode-go/model", "state": "unknown"}], True),
                 ([{"model": "opencode-go/model", "state": "unavailable"}], False))
        for rows, expected in cases:
            with self.subTest(rows=rows), mock.patch.object(
                    rt, "provider_settings", return_value={"model": "m/x", "budget": "high"}), \
                 mock.patch.object(rt.opencode_runtime, "automatic_candidates",
                                   return_value=rows):
                self.assertEqual(rt._opencode_models_eligible("deep"), expected)

    def test_cascade_passes_each_child_context_without_mutating_caller(self):
        env = {"CODEX_HOME": "caller", "OPENCODE_CONFIG_CONTENT": "foreign", "KEEP": "value"}
        command = rt._ProviderCommand(["provider"], "prompt", None,
                                      env={"CODEX_HOME": "isolated", "OPENCODE_CONFIG_CONTENT": None},
                                      cwd="neutral")
        with mock.patch.object(rt.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="reply")) as run:
            self.assertEqual(rt.run_provider_cascade([command], timeout=1, env=env, cwd="caller"), ("reply", 0))
        self.assertEqual(run.call_args.kwargs["cwd"], "neutral")
        self.assertEqual(run.call_args.kwargs["env"], {"CODEX_HOME": "isolated", "KEEP": "value"})
        self.assertEqual(env["CODEX_HOME"], "caller")
        self.assertIn("OPENCODE_CONFIG_CONTENT", env)

    def test_opencode_refreshes_existing_generated_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".opencode/agent/fleet-titler.md"
            path.parent.mkdir(parents=True)
            path.write_text("old per-tool deny list")
            rt._opencode_workdir("unused", workdir=tmp)
            self.assertEqual(path.read_text(), rt._OPENCODE_AGENT)


if __name__ == "__main__":
    unittest.main()
