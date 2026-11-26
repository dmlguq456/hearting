"""Codex config keys Hearting sets only where the user has not (PG-13, PG-23).

Covers `_codex_table_keys` (one key at a time) / `_codex_table_keys_remove` and their
use in `_merge_codex_managed_keys`, the sub-agent default's model-list rule, and the
design server's per-run approval; every case stays in text or a temporary directory.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import json
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/install"))
sys.path.insert(0, str(ROOT / "utilities"))
import runtime_activation as ra
import codex_permission_profile

DESIGN = ("mcp_servers", "design")
APPROVE = {"default_tools_approval_mode": "approve"}
DEFAULT = {"default_subagent_model": "gpt-6-luna", "default_subagent_reasoning_effort": "medium"}
REGISTERED = '[mcp_servers.design]\ncommand = "node"\nargs = ["server.js"]\n\n[mcp_servers.other]\ncommand = "x"\n'
AGENTS = ("agents",)


def keys(text, table, values, previous=None, create=True):
    return ra._codex_table_keys(text, table, values, previous, create_table=create)


class TableKeyTests(unittest.TestCase):
    def test_registered_table_gets_keys_and_removes_back_to_user_bytes(self):
        text, record = keys(REGISTERED, DESIGN, APPROVE, create=False)
        data = tomllib.loads(text)
        self.assertEqual(data["mcp_servers"]["design"]["default_tools_approval_mode"], "approve")
        self.assertNotIn("default_tools_approval_mode", data["mcp_servers"]["other"])
        self.assertEqual(ra._codex_table_keys_remove(text, record), REGISTERED)

    def test_missing_table_is_not_created_without_create(self):
        self.assertEqual(keys('model = "m"\n', DESIGN, APPROVE, create=False), ('model = "m"\n', None))

    def test_user_value_is_never_overwritten_or_removed(self):
        user = REGISTERED.replace('args = ["server.js"]\n', 'args = ["server.js"]\ndefault_tools_approval_mode = "prompt"\n')
        self.assertEqual(keys(user, DESIGN, APPROVE, create=False), (user, None))
        forged = {"table": list(DESIGN), "values": APPROVE, "lines": 'default_tools_approval_mode = "approve"\n', "header": ""}
        self.assertEqual(ra._codex_table_keys_remove(user, forged), user)

    def test_receipt_is_carried_while_its_lines_still_hold(self):
        text, record = keys(REGISTERED, AGENTS, DEFAULT)
        again, carried = keys(text, AGENTS, DEFAULT, record)
        self.assertEqual((again, carried), (text, record))

    def test_agents_table_created_and_removed_whole(self):
        original = 'model = "gpt-6.1"\n\n[agents.light]\nconfig_file = "agents/light.toml"\n'
        text, record = keys(original, AGENTS, DEFAULT)
        agents = tomllib.loads(text)["agents"]
        self.assertEqual(agents["default_subagent_model"], "gpt-6-luna")
        self.assertEqual(agents["light"]["config_file"], "agents/light.toml")
        self.assertEqual(ra._codex_table_keys_remove(text, record), original)

    def test_existing_agents_table_keeps_user_keys(self):
        original = '[agents]\nmax_threads = 4\n'
        text, record = keys(original, AGENTS, DEFAULT)
        agents = tomllib.loads(text)["agents"]
        self.assertEqual((agents["max_threads"], agents["default_subagent_model"]), (4, "gpt-6-luna"))
        self.assertEqual(ra._codex_table_keys_remove(text, record), original)

    def test_user_default_model_blocks_the_pair(self):
        original = '[agents]\ndefault_subagent_model = "mine"\n'
        self.assertEqual(keys(original, AGENTS, DEFAULT), (original, None))

    def test_inline_or_dotted_forms_stay_untouched(self):
        for original, table, values in (('agents = { max_threads = 2 }\n', AGENTS, DEFAULT),
                                        ('mcp_servers.design.command = "node"\n', DESIGN, APPROVE)):
            self.assertEqual(keys(original, table, values), (original, None))

    def test_one_user_edited_key_keeps_the_other_owned(self):
        # F2: the user changes only the effort; the model line stays Hearting's.
        text, record = keys('model = "m"\n', AGENTS, DEFAULT)
        edited = text.replace('default_subagent_reasoning_effort = "medium"',
                              'default_subagent_reasoning_effort = "high"')
        carried_text, carried = keys(edited, AGENTS, DEFAULT, record)
        self.assertEqual(carried_text, edited)
        self.assertEqual(sorted(carried["keys"]), ["default_subagent_model"])
        withdrawn, after = keys(edited, AGENTS, None, carried)
        self.assertEqual(tomllib.loads(withdrawn)["agents"],
                         {"default_subagent_reasoning_effort": "high"})
        self.assertIsNone(after)
        self.assertEqual(ra._codex_table_keys_remove(edited, carried),
                         withdrawn)

    def test_earlier_whole_block_receipt_settles_per_key(self):
        text, _record = keys('model = "m"\n', AGENTS, DEFAULT)
        old = {"table": ["agents"], "values": DEFAULT, "header": "\n[agents]\n",
               "lines": "".join(f'{k} = {json.dumps(v)}\n' for k, v in DEFAULT.items())}
        edited = text.replace('"medium"', '"high"')
        withdrawn, after = keys(edited, AGENTS, None, old)
        self.assertNotIn("default_subagent_model", tomllib.loads(withdrawn)["agents"])
        self.assertIsNone(after)


class ManagedKeysTests(unittest.TestCase):
    def merge(self, text, default, previous=None):
        with patch.object(ra, "_native_agent_default", return_value=default):
            return ra._merge_codex_managed_keys(text, ROOT, "global", previous or {})

    def test_design_server_approval_is_not_written_to_the_config(self):
        # PG-23: the value would also remove interactive sessions' approval question.
        text, records = self.merge(REGISTERED, DEFAULT)
        data = tomllib.loads(text)
        self.assertNotIn("default_tools_approval_mode", data["mcp_servers"]["design"])
        self.assertEqual(data["agents"]["default_subagent_model"], "gpt-6-luna")
        self.assertEqual(sorted(records), ["subagent_default"])

    def test_earlier_design_approval_record_is_withdrawn(self):
        text, record = keys(REGISTERED, DESIGN, APPROVE, create=False)
        later, after = self.merge(text, None, {"design_mcp_approval": record})
        self.assertEqual((later, after), (REGISTERED, {}))

    def test_unconfirmed_default_withdraws_only_the_recorded_default(self):
        text, records = self.merge(REGISTERED, DEFAULT)
        later, after = self.merge(text, None, records)
        self.assertEqual(later, REGISTERED)
        self.assertEqual(after, {})

    def test_changed_default_replaces_the_recorded_one(self):
        text, records = self.merge('model = "m"\n', DEFAULT)
        other = {**DEFAULT, "default_subagent_model": "gpt-6-luna-2"}
        later, after = self.merge(text, other, records)
        self.assertEqual(tomllib.loads(later)["agents"]["default_subagent_model"], "gpt-6-luna-2")
        self.assertEqual(after["subagent_default"]["keys"]["default_subagent_model"]["value"],
                         "gpt-6-luna-2")


def _catalog(home: Path, slugs, name="catalog.json", efforts=("low", "medium", "high"), **fields):
    entry = {"supported_in_api": True, "multi_agent_version": "v1", **fields}
    (home / name).write_text(json.dumps({"models": [
        {"slug": slug, "supported_reasoning_levels": [{"effort": e} for e in efforts], **entry}
        for slug in slugs]}), encoding="utf-8")


class CodexAgentFileTests(unittest.TestCase):
    def test_codex_agents_are_written_as_files_not_links(self):
        # Codex 0.163.0 refuses a linked role file ("agent type is currently not available").
        with tempfile.TemporaryDirectory(prefix="codex-agent-files-") as temp:
            home = Path(temp)
            (home / "config.toml").write_text('model = "gpt-6.1"\n', encoding="utf-8")
            entries, _lines = ra._native_agent_entries("codex", ROOT, home)
            self.assertTrue(entries)
            for entry in entries:
                self.assertEqual(entry["kind"], "render", entry["dest"])
                self.assertEqual(entry["content"],
                                 (ROOT / "adapters/codex/agents" / Path(entry["dest"]).name).read_text())


class SubagentDefaultListTests(unittest.TestCase):
    """F1: the default is written only where the home's model lists carry it."""

    def default(self, config, setup=lambda home: None):
        with tempfile.TemporaryDirectory(prefix="codex-default-list-") as temp:
            home = Path(temp)
            (home / "config.toml").write_text(config, encoding="utf-8")
            setup(home)
            return ra._native_agent_default("codex", ROOT, home)

    CATALOG = 'model = "gpt-6.1"\nmodel_catalog_json = "catalog.json"\n'

    def test_catalog_accepting_the_light_model_confirms(self):
        self.assertEqual(self.default(self.CATALOG, lambda h: _catalog(h, ["gpt-6.1", "gpt-6-luna"])),
                         DEFAULT)

    def test_api_unsupported_model_writes_nothing(self):
        # R2 counterexample: an API-key session drops it from the spawn list.
        for value in (False, None, "true"):
            self.assertIsNone(self.default(self.CATALOG, lambda h: _catalog(
                h, ["gpt-6-luna"], supported_in_api=value)), value)

    def test_models_cache_is_never_the_evidence(self):
        # Codex adopts the cache only within its TTL and for its own version/identity.
        fresh = {"fetched_at": "2999-01-01T00:00:00Z", "client_version": "0.163.0", "identity": "x"}
        for extra in ({}, {"fetched_at": "2000-01-01T00:00:00Z"}, {"client_version": "0.0.1"}, fresh):
            def setup(h, extra=extra):
                _catalog(h, ["gpt-6-luna"], "models_cache.json")
                document = json.loads((h / "models_cache.json").read_text())
                (h / "models_cache.json").write_text(json.dumps({**document, **extra}))
            self.assertIsNone(self.default('model = "gpt-6.1"\n', setup), extra)

    def test_no_readable_list_writes_nothing(self):
        self.assertIsNone(self.default(self.CATALOG))
        self.assertIsNone(self.default(self.CATALOG, lambda h: (h / "catalog.json").write_text("{")))

    def test_custom_catalog_without_light_writes_nothing(self):
        self.assertIsNone(self.default(self.CATALOG, lambda h: (
            _catalog(h, ["catalog-only-model"]),
            _catalog(h, ["gpt-6-luna"], "models_cache.json"))))

    def test_every_session_list_must_accept_model_and_effort(self):
        config = self.CATALOG + '[profiles.other]\nmodel_catalog_json = "other.json"\n'
        for other in ({}, {"efforts": ("low",)}, {"multi_agent_version": "disabled"},
                      {"supported_in_api": False}):
            self.assertIsNone(self.default(config, lambda h, other=other: (
                _catalog(h, ["gpt-6-luna"]), _catalog(h, ["gpt-6-luna"] if other else ["x"],
                                                      "other.json", **other))), other)
        self.assertEqual(self.default(config, lambda h: (
            _catalog(h, ["gpt-6-luna"]), _catalog(h, ["gpt-6-luna"], "other.json"))), DEFAULT)
        # The top-level session has no catalog and so uses the unconfirmable cache.
        self.assertIsNone(self.default('model = "gpt-6.1"\n[profiles.other]\nmodel_catalog_json = "catalog.json"\n',
                                       lambda h: (_catalog(h, ["gpt-6-luna"]),
                                                  _catalog(h, ["gpt-6-luna"], "models_cache.json"))))

    def test_listed_model_without_the_effort_writes_nothing(self):
        for efforts in (("low",), ()):
            self.assertIsNone(self.default(self.CATALOG, lambda h, efforts=efforts: _catalog(
                h, ["gpt-6-luna"], efforts=efforts)), efforts)

    def test_other_provider_still_writes_nothing(self):
        self.assertIsNone(self.default('model_provider = "other"\nmodel_catalog_json = "catalog.json"\n',
                                       lambda h: _catalog(h, ["gpt-6-luna"])))


class DesignRunArgumentTests(unittest.TestCase):
    """PG-23: the design server's approval is a per-run argument, never config."""

    def arguments(self, config):
        with tempfile.TemporaryDirectory(prefix="codex-design-run-") as temp:
            (Path(temp) / "config.toml").write_text(config, encoding="utf-8")
            return codex_permission_profile.design_mcp_run_arguments(temp)

    def test_registered_server_gets_one_run_argument(self):
        self.assertEqual(self.arguments(REGISTERED),
                         ["-c", 'mcp_servers.design.default_tools_approval_mode="approve"'])

    def test_unregistered_disabled_or_user_set_gets_none(self):
        for config in ('model = "m"\n', REGISTERED.replace('args', 'enabled = false\nargs'),
                       REGISTERED.replace('args', 'default_tools_approval_mode = "prompt"\nargs'),
                       '[mcp_servers.design]\nenabled = true\n', "not toml ["):
            with self.subTest(config=config):
                self.assertEqual(self.arguments(config), [])


if __name__ == "__main__":
    unittest.main()
