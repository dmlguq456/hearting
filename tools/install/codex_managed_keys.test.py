"""Codex config keys Hearting sets only where the user has not (PG-13, PG-23).

Covers `_codex_table_keys_insert` / `_codex_table_keys_remove` and their use in
`_merge_codex_managed_keys`; every case stays in text or a temporary directory.
"""
from pathlib import Path
import sys
import tomllib
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/install"))
import runtime_activation as ra

DESIGN = ("mcp_servers", "design")
APPROVE = {"default_tools_approval_mode": "approve"}
DEFAULT = {"default_subagent_model": "gpt-6-luna", "default_subagent_reasoning_effort": "medium"}
REGISTERED = '[mcp_servers.design]\ncommand = "node"\nargs = ["server.js"]\n\n[mcp_servers.other]\ncommand = "x"\n'


class TableKeyTests(unittest.TestCase):
    def test_design_server_gets_approval_and_removes_back_to_user_bytes(self):
        text, record = ra._codex_table_keys_insert(REGISTERED, DESIGN, APPROVE, None, create_table=False)
        data = tomllib.loads(text)
        self.assertEqual(data["mcp_servers"]["design"]["default_tools_approval_mode"], "approve")
        self.assertNotIn("default_tools_approval_mode", data["mcp_servers"]["other"])
        self.assertEqual(ra._codex_table_keys_remove(text, record), REGISTERED)

    def test_unregistered_design_server_is_not_created(self):
        text, record = ra._codex_table_keys_insert('model = "m"\n', DESIGN, APPROVE, None, create_table=False)
        self.assertEqual((text, record), ('model = "m"\n', None))

    def test_user_value_is_never_overwritten_or_removed(self):
        user = REGISTERED.replace('args = ["server.js"]\n', 'args = ["server.js"]\ndefault_tools_approval_mode = "prompt"\n')
        text, record = ra._codex_table_keys_insert(user, DESIGN, APPROVE, None, create_table=False)
        self.assertEqual((text, record), (user, None))
        forged = {"table": list(DESIGN), "values": APPROVE, "lines": 'default_tools_approval_mode = "approve"\n', "header": ""}
        self.assertEqual(ra._codex_table_keys_remove(user, forged), user)

    def test_receipt_is_carried_while_its_lines_still_hold(self):
        text, record = ra._codex_table_keys_insert(REGISTERED, DESIGN, APPROVE, None, create_table=False)
        again, carried = ra._codex_table_keys_insert(text, DESIGN, APPROVE, record, create_table=False)
        self.assertEqual((again, carried), (text, record))

    def test_agents_table_created_and_removed_whole(self):
        original = 'model = "gpt-6.1"\n\n[agents.light]\nconfig_file = "agents/light.toml"\n'
        text, record = ra._codex_table_keys_insert(original, ("agents",), DEFAULT, None, create_table=True)
        agents = tomllib.loads(text)["agents"]
        self.assertEqual(agents["default_subagent_model"], "gpt-6-luna")
        self.assertEqual(agents["light"]["config_file"], "agents/light.toml")
        self.assertEqual(ra._codex_table_keys_remove(text, record), original)

    def test_existing_agents_table_keeps_user_keys(self):
        original = '[agents]\nmax_threads = 4\n'
        text, record = ra._codex_table_keys_insert(original, ("agents",), DEFAULT, None, create_table=True)
        agents = tomllib.loads(text)["agents"]
        self.assertEqual((agents["max_threads"], agents["default_subagent_model"]), (4, "gpt-6-luna"))
        self.assertEqual(ra._codex_table_keys_remove(text, record), original)

    def test_user_default_model_blocks_the_pair(self):
        original = '[agents]\ndefault_subagent_model = "mine"\n'
        self.assertEqual(ra._codex_table_keys_insert(original, ("agents",), DEFAULT, None, create_table=True),
                         (original, None))

    def test_inline_or_dotted_forms_stay_untouched(self):
        for original, table, values in (('agents = { max_threads = 2 }\n', ("agents",), DEFAULT),
                                        ('mcp_servers.design.command = "node"\n', DESIGN, APPROVE)):
            self.assertEqual(ra._codex_table_keys_insert(original, table, values, None, create_table=True),
                             (original, None))


class ManagedKeysTests(unittest.TestCase):
    def merge(self, text, default, previous=None):
        with patch.object(ra, "_native_agent_default", return_value=default):
            return ra._merge_codex_managed_keys(text, ROOT, "global", previous or {})

    def test_both_keys_on_an_openai_home(self):
        text, records = self.merge(REGISTERED, DEFAULT)
        data = tomllib.loads(text)
        self.assertEqual(data["mcp_servers"]["design"]["default_tools_approval_mode"], "approve")
        self.assertEqual(data["agents"]["default_subagent_model"], "gpt-6-luna")
        self.assertEqual(sorted(records), ["design_mcp_approval", "subagent_default"])

    def test_provider_change_withdraws_only_the_recorded_default(self):
        text, records = self.merge(REGISTERED, DEFAULT)
        later, after = self.merge(text, None, records)
        self.assertNotIn("agents", tomllib.loads(later))
        self.assertEqual(sorted(after), ["design_mcp_approval"])

    def test_changed_default_replaces_the_recorded_one(self):
        text, records = self.merge('model = "m"\n', DEFAULT)
        other = {**DEFAULT, "default_subagent_model": "gpt-6-luna-2"}
        later, after = self.merge(text, other, records)
        self.assertEqual(tomllib.loads(later)["agents"]["default_subagent_model"], "gpt-6-luna-2")
        self.assertEqual(after["subagent_default"]["values"], other)


class CodexAgentFileTests(unittest.TestCase):
    def test_codex_agents_are_written_as_files_not_links(self):
        # Codex 0.163.0 refuses a linked role file ("agent type is currently not available").
        import tempfile
        with tempfile.TemporaryDirectory(prefix="codex-agent-files-") as temp:
            home = Path(temp)
            (home / "config.toml").write_text('model = "gpt-6.1"\n', encoding="utf-8")
            entries, _lines = ra._native_agent_entries("codex", ROOT, home)
            self.assertTrue(entries)
            for entry in entries:
                self.assertEqual(entry["kind"], "render", entry["dest"])
                self.assertEqual(entry["content"],
                                 (ROOT / "adapters/codex/agents" / Path(entry["dest"]).name).read_text())
            self.assertEqual(ra._native_agent_default("codex", ROOT, home), DEFAULT)
            (home / "config.toml").write_text('model_provider = "other"\n', encoding="utf-8")
            self.assertIsNone(ra._native_agent_default("codex", ROOT, home))


if __name__ == "__main__":
    unittest.main()
