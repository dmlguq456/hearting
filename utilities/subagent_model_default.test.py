"""The native sub-agent role default each runtime realizes (PG-13)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import model_profile  # noqa: E402
import native_agents  # noqa: E402


def shipped(adapter):
    return model_profile.load_config(ROOT / "adapters" / adapter / "config" / "models.conf")


class NativeDefaultTests(unittest.TestCase):
    def test_light_profile_per_adapter(self):
        self.assertEqual(native_agents.native_subagent_default("codex", shipped("codex"))["model"], "gpt-6-luna")
        self.assertEqual(native_agents.native_subagent_default("claude", shipped("claude"))["model"], "sonnet")
        self.assertTrue(native_agents.native_subagent_default("opencode", shipped("opencode"))["model"]
                        .startswith("opencode-go/"))

    def test_main_session_only_model_is_never_the_default(self):
        cfg = dict(shipped("codex"))
        cfg["CFG_MAIN_SESSION_ONLY_MODELS"] = "gpt-6-luna"
        self.assertIsNone(native_agents.native_subagent_default("codex", cfg))

    def test_codex_plan_carries_the_default_only_where_the_home_lists_it(self):
        renderer = ROOT / "adapters/codex/bin/sync-native-agents.py"
        cache = json.dumps({"models": [{"slug": "gpt-6-luna"}]})
        with tempfile.TemporaryDirectory(prefix="subagent-default-") as raw:
            home = Path(raw)
            for text, listed, expected in (('model = "gpt-6.1"\n', cache, "gpt-6-luna"),
                                           ('model = "gpt-6.1"\n', None, None),
                                           ('model_provider = "ollama"\n', cache, None)):
                (home / "config.toml").write_text(text, encoding="utf-8")
                cache_path = home / "models_cache.json"
                cache_path.unlink(missing_ok=True)
                if listed:
                    cache_path.write_text(listed, encoding="utf-8")
                plan = json.loads(subprocess.run([sys.executable, "-B", str(renderer), "--runtime-home", str(home),
                                                  "--json"], text=True, capture_output=True, check=True).stdout)
                value = plan["default_subagent"]
                self.assertEqual(value and value["default_subagent_model"], expected)


class OpenCodeDefaultTests(unittest.TestCase):
    """PG-13 limit: OpenCode built-ins keep the parent session's model (F3).

    The plugin's only places to set a built-in's model are process-wide and do not
    see the parent's actual model, so the `config` hook helper returns the user's
    built-in entries as they are, whatever provider the file's `model` names.
    """

    def test_config_hook_leaves_builtin_subagents_inheriting(self):
        with tempfile.TemporaryDirectory(prefix="subagent-oc-") as raw:
            home = Path(raw)
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home),
                   "XDG_CONFIG_HOME": str(home / ".config")}
            values = dict(shipped("opencode"))
            provider = native_agents.native_subagent_default("opencode", values)["model"].split("/", 1)[0]
            for config in ({"model": f"{provider}/main"},
                           {"model": f"{provider}/main", "agent": {"general": {"prompt": "x"}}}):
                done = subprocess.run([sys.executable, "-B", str(ROOT / "utilities/opencode_context_window.py")],
                                      input=json.dumps({"config": config}) + "\n", text=True,
                                      capture_output=True, env=env, timeout=60)
                out = json.loads(done.stdout)
                self.assertNotIn("subagent_default", out)
                for key in ("agent", "agents"):
                    for agent in ("general", "explore"):
                        self.assertNotIn("model", (out["config"].get(key) or {}).get(agent) or {})

    def test_plugin_sets_no_builtin_model(self):
        plugin = (ROOT / "adapters/opencode/plugins/hearting-guards.js").read_text(encoding="utf-8")
        self.assertNotIn("agent.transform", plugin)
        self.assertNotIn("subagent_default", plugin)


if __name__ == "__main__":
    unittest.main()
