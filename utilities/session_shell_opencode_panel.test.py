"""OpenCode uses the same right HEARTING panel as Claude and Codex."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("shell_opencode_entry", ROOT / "utilities/session-shell/entry.py")
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


class NativeSidebarTest(unittest.TestCase):
    def content(self, environment):
        return json.loads(environment["OPENCODE_CLI_CONFIG_CONTENT"])

    def test_the_shell_window_hides_opencode_sidebar_through_its_cli_config_merge(self):
        original = {"KEEP": "1"}
        result = entry.native_environment("opencode", original)
        self.assertEqual(self.content(result), {"session": {"sidebar": "hide"}})
        self.assertEqual(original, {"KEEP": "1"}, "the caller's environment is unchanged")
        merged = entry.native_environment("opencode", {"OPENCODE_CLI_CONFIG_CONTENT": '{"session":{"thinking":"show"},"mouse":false}'})
        self.assertEqual(self.content(merged), {"session": {"thinking": "show", "sidebar": "hide"}, "mouse": False})

    def test_an_explicit_choice_other_harnesses_and_unreadable_content_are_left_alone(self):
        for harness in ("claude", "codex"):
            self.assertEqual(entry.native_environment(harness, {}), {})
        for raw in ('{"session":{"sidebar":"auto"}}', "not json", "[]", '{"session":"odd"}'):
            environment = {"OPENCODE_CLI_CONFIG_CONTENT": raw}
            self.assertIs(entry.native_environment("opencode", environment), environment)

    def test_layout_has_no_opencode_exception(self):
        script = r"""const assert=require('node:assert/strict'),d=require(process.argv[1]);
const configuration={layout:'side',side_width:40,side_min_columns:100,narrow:'bottom',auxiliary:'summary',bottom_rows:14};
const wide=d.layout({cols:133,rows:30,configuration});
assert.equal(wide.side,true);assert.equal(wide.panelCols,40);assert.equal(wide.nativeCols,91);
assert.equal(d.layout({cols:80,rows:24,configuration}).side,false);
"""
        result = subprocess.run(["node", "-e", script, str(ROOT / "utilities/session-shell/native-display.cjs")],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


class McpCountTest(unittest.TestCase):
    def test_compact_status_keeps_the_hidden_sidebar_mcp_count(self):
        from tools.fleet.session_panel import compact_status
        text = lambda rows: "".join(part[0] for row in rows for part in row)
        values = {"ctx_pct": 10, "quota_windows": [("5h", 1, None)]}
        self.assertNotIn("MCP", text(compact_status(values, 40)))
        failed = compact_status({**values, "mcp": {"total": 2, "connected": 1, "failed": 1}}, 40)
        self.assertIn("MCP 1/2", text(failed))
        self.assertEqual(failed[0][-1][1], "lvl_r")
        self.assertEqual(compact_status({**values, "mcp": {"total": 1, "connected": 1, "failed": 0}}, 40)[0][-1][1], "lvl_g")
        self.assertNotIn("MCP", text(compact_status({**values, "mcp": {"total": 0, "connected": 0, "failed": 0}}, 40)))


if __name__ == "__main__":
    unittest.main()
