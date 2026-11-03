#!/usr/bin/env python3
"""Behavioral regressions for Codex status-line merge and feasibility."""
import json
from pathlib import Path
import os
import stat
import tempfile
import tomllib
import unittest

import codex_tui_config as config

FRAGMENT = Path(__file__).resolve().parents[1] / "adapters/codex/config/tui-statusline.toml"
# The Hearting default before thread-id moved first (narrow panes clipped it).
FORMER_DEFAULT = ["thread-title", "git-branch", "context-used", "model-with-reasoning",
                  "five-hour-limit", "weekly-limit", "thread-id"]
NEW_DEFAULT = ["thread-id", "thread-title", "git-branch", "context-used", "model-with-reasoning",
               "five-hour-limit", "weekly-limit"]

class CodexTuiConfigTest(unittest.TestCase):
    def _tracked_merge(self, text, previous=None):
        merged = config.merge_status_line_text(text)
        return merged, config.status_line_record(text, merged, previous)

    def test_restore_clean_and_existing_config_after_refresh(self):
        for source in ('', 'model = "mine"\n',
                       '# mine\n[tui]\nstatus_line = ["x"] # footer\nstatus_line_use_colors = false\n',
                       '[tui]\nstatus_line = ["thread-id", "x"]\n',
                       '[tui]\nstatus_line = []\n',
                       '[tui]\nnotification_method = "osc9"\n'):
            with self.subTest(source=source):
                merged, record = self._tracked_merge(source)
                refreshed, record = self._tracked_merge(merged, record)
                self.assertEqual(config.restore_status_line_text(refreshed, record), source)

    def test_restore_keeps_later_items_colors_and_other_tables(self):
        merged, record = self._tracked_merge('[tui]\nstatus_line = ["x"]\n')
        edited = merged.replace('["thread-id", "x"]', '["thread-id", "x", "new"]')
        edited = edited.replace('status_line_use_colors = true', 'status_line_use_colors = false')
        edited += '\n[projects."/work"]\ntrust_level = "trusted"\n'
        for refresh in (False, True):
            with self.subTest(refresh=refresh):
                current, receipt = self._tracked_merge(edited, record) if refresh else (edited, record)
                restored = config.restore_status_line_text(current, receipt)
                tui = tomllib.loads(restored)["tui"]
                self.assertEqual(tui["status_line"], ["x", "new"])
                self.assertIs(tui["status_line_use_colors"], False)
                self.assertIn('[projects."/work"]\ntrust_level = "trusted"\n', restored)

    def test_restore_keeps_user_owned_thread_id_and_new_table_settings(self):
        merged, record = self._tracked_merge('[tui]\nstatus_line = ["thread-id", "x"]\n')
        edited = merged.replace('["thread-id", "x"]', '["thread-id", "new"]')
        self.assertNotEqual(edited, merged)
        self.assertEqual(tomllib.loads(config.restore_status_line_text(edited, record))["tui"]["status_line"],
                         ["thread-id", "new"])
        merged, record = self._tracked_merge('')
        edited = merged + 'notification_method = "osc9"\n'
        self.assertEqual(tomllib.loads(config.restore_status_line_text(edited, record)),
                         {"tui": {"notification_method": "osc9"}})

    def test_missing_receipt_does_not_claim_legacy_settings(self):
        source = config.merge_status_line_text('')
        self.assertEqual(config.restore_status_line_text(source, None), source)
        for invalid in ({"keys": {}}, {"keys": {"status_line": {"baseline": {"present": False}}}}):
            self.assertEqual(config.restore_status_line_text(source, invalid), source)

    def test_new_keys_stay_outside_the_next_tables_managed_comment(self):
        for settings in ('', 'status_line = ["custom"]\n', 'status_line_use_colors = false\n'):
            source = '[tui]\n' + settings + '\n# >>> managed block\n[other]\nvalue = true\n'
            merged, record = self._tracked_merge(source)
            self.assertIn('\n# >>> managed block\n[other]\nvalue = true\n', merged)
            self.assertEqual(config.restore_status_line_text(merged, record), source)

    def test_existing_items_and_comments_survive_with_thread_id_first(self):
        source = ('# keep this comment\n[tui] # preserve header\n'
                  'status_line = [\n  "project-name", # item comment\n'
                  '  "thread-id", "custom-item",\n]\n'
                  'status_line_use_colors = false # explicit color\n\n[projects."/tmp/example"]\n'
                  'trust_level = "trusted"\n')
        merged = config.merge_status_line_text(source)
        parsed = tomllib.loads(merged)
        self.assertEqual(parsed["tui"]["status_line"],
                         ["thread-id", "project-name", "custom-item"])
        self.assertIs(parsed["tui"]["status_line_use_colors"], False)
        self.assertIn("# keep this comment", merged)
        self.assertIn("# preserve header", merged)
        self.assertIn("# explicit color", merged)
        self.assertIn('[projects."/tmp/example"]', merged)
        self.assertEqual(config.merge_status_line_text(merged), merged)

    def test_absent_table_key_empty_and_duplicates_converge(self):
        self.assertEqual(tomllib.loads(config.merge_status_line_text(""))["tui"]["status_line"][0],
                         "thread-id")
        self.assertEqual(tomllib.loads(config.merge_status_line_text(
            '[tui]\nstatus_line_use_colors = false\n'))["tui"]["status_line"][0], "thread-id")
        for source in ('[tui]\nstatus_line = []\n',
                       '[tui]\nstatus_line = ["thread-id", "x", "thread-id"]\n'):
            with self.subTest(source=source):
                merged = config.merge_status_line_text(source)
                items = tomllib.loads(merged)["tui"]["status_line"]
                self.assertEqual(items, (["thread-id", "x"] if '"x"' in source else ["thread-id"]))

    def test_default_order_leads_with_thread_id(self):
        self.assertEqual(list(config.DEFAULT_STATUS_LINE), NEW_DEFAULT)
        fragment = tomllib.loads(FRAGMENT.read_text(encoding="utf-8"))["tui"]
        self.assertEqual(fragment["status_line"], NEW_DEFAULT)
        self.assertEqual(tomllib.loads(config.merge_status_line_text(""))["tui"]["status_line"],
                         NEW_DEFAULT)
        self.assertEqual(tomllib.loads(config.merge_status_line_from_fragment("", FRAGMENT))
                         ["tui"]["status_line"], NEW_DEFAULT)

    def test_former_default_moves_thread_id_first_and_keeps_every_other_byte(self):
        former = "status_line = " + json.dumps(FORMER_DEFAULT) + " # footer"
        current = "status_line = " + json.dumps(NEW_DEFAULT) + " # footer"
        source = ('model = "keep"\n# user comment\n[tui]\n' + former + '\n'
                  'status_line_use_colors = false\nnotification_method = "osc9"\n\n'
                  '[projects."/work"]\ntrust_level = "trusted"\n')
        merged = config.merge_status_line_from_fragment(source, FRAGMENT)
        self.assertEqual(merged, source.replace(former, current))
        self.assertEqual(config.merge_status_line_from_fragment(merged, FRAGMENT), merged)

    def test_former_default_install_refreshes_and_still_restores(self):
        # An install recorded the former default as its own projection.
        for source in ("", 'model = "keep"\n'):
            with self.subTest(source=source):
                # What the earlier release wrote: the same bytes with the former order.
                former = config.merge_status_line_text(source).replace(
                    json.dumps(NEW_DEFAULT), json.dumps(FORMER_DEFAULT))
                record = config.status_line_record(source, former)
                self.assertEqual(record["keys"]["status_line"]["projected"], FORMER_DEFAULT)
                refreshed, record = self._tracked_merge(former, record)
                self.assertEqual(tomllib.loads(refreshed)["tui"]["status_line"], NEW_DEFAULT)
                self.assertEqual(config.restore_status_line_text(refreshed, record), source)

    def test_user_list_keeps_its_items_order_and_other_bytes(self):
        tail = ('status_line_use_colors = false\n\n[hooks.state]\n"example" = "keep"\n')
        lead = 'model = "keep"\n[tui]\n'
        # A list that already leads with thread-id is left byte for byte.
        for items in (["thread-id"], ["thread-id", "project-name", "weekly-limit"]):
            with self.subTest(items=items):
                source = lead + "status_line = " + json.dumps(items) + "\n" + tail
                self.assertEqual(config.merge_status_line_from_fragment(source, FRAGMENT), source)
        # A customised list keeps its own items and order; only the one managed
        # identity item takes the leading place.
        custom = ["project-name", "git-branch", "context-used", "model-with-reasoning",
                  "weekly-limit", "context-window-size", "approval-mode"]
        for items in (custom + ["thread-id"], custom):
            with self.subTest(items=items):
                line = "status_line = " + json.dumps(items)
                source = lead + line + "\n" + tail
                merged = config.merge_status_line_from_fragment(source, FRAGMENT)
                self.assertEqual(merged, source.replace(
                    line, "status_line = " + json.dumps(["thread-id"] + custom)))
                merged, record = self._tracked_merge(source)
                self.assertEqual(config.restore_status_line_text(merged, record), source)

    def test_one_item_per_line_array_keeps_its_comments_and_layout(self):
        array = ('status_line = [ # footer\n  "project-name", # mine\n\n  # note\n'
                 "  'git-branch',\n" + '  "thread-id", # managed\n  "weekly-limit" # last\n] # end\n')
        source = '# head\n[tui]\n' + array + 'status_line_use_colors = false\n\n[other]\nx = 1\n'
        merged = config.merge_status_line_text(source)
        self.assertEqual(merged, source.replace(array, (
            'status_line = [ # footer\n  "thread-id",\n  "project-name", # mine\n\n  # note\n'
            "  'git-branch',\n" + '  "weekly-limit" # last\n] # end\n')))
        self.assertEqual(tomllib.loads(merged)["tui"]["status_line"],
                         ["thread-id", "project-name", "git-branch", "weekly-limit"])
        self.assertEqual(config.merge_status_line_text(merged), merged)
        merged, record = self._tracked_merge(source)
        self.assertEqual(config.restore_status_line_text(merged, record), source)
        # Without the managed item the line is added on top in the same layout.
        bare = source.replace('  "thread-id", # managed\n', "")
        self.assertIn('status_line = [ # footer\n  "thread-id",\n  "project-name", # mine\n',
                      config.merge_status_line_text(bare))

    def test_startup_default_preserves_explicit_values_and_inserts_at_toml_root(self):
        for value in (True, False):
            source = f'"check_for_update_on_startup" = {str(value).lower()} # user choice\r\n[tui]\r\n'
            with self.subTest(value=value):
                self.assertEqual(config.merge_startup_update_default_text(source), source)
        for source in (
                "",
                '# check_for_update_on_startup = true\n[tui]\n',
                '[tui]\nstatus_line = []\n',
                'description = """\n[not_a_table]\n"""\r\n[tui]\r\n'):
            with self.subTest(source=source):
                merged = config.merge_startup_update_default_text(source)
                parsed = tomllib.loads(merged)
                self.assertIs(parsed["check_for_update_on_startup"], False)
                self.assertIn("check_for_update_on_startup = false", merged)
                self.assertEqual(config.merge_startup_update_default_text(merged), merged)
                if "[tui]" in source:
                    self.assertLess(merged.index("check_for_update_on_startup = false"),
                                    merged.index("[tui]"))
        self.assertIn("check_for_update_on_startup = false\r\n[tui]\r\n",
                      config.merge_startup_update_default_text("[tui]\r\n"))

    def test_startup_default_receipt_restores_only_our_unedited_root_insertion(self):
        for source in ("", 'model = "keep"\n[tui]\n', '[tui]\r\n'):
            with self.subTest(source=source):
                merged = config.merge_startup_update_default_text(source)
                record = config.startup_update_default_record(source, merged)
                self.assertTrue(record)
                carried = config.startup_update_default_record(merged, merged, record)
                self.assertEqual(carried, record)
                self.assertEqual(config.restore_startup_update_default_text(merged, carried), source)

        merged = config.merge_startup_update_default_text('[tui]\n')
        record = config.startup_update_default_record('[tui]\n', merged)
        changed_value = merged.replace("check_for_update_on_startup = false",
                                       "check_for_update_on_startup = true")
        changed_line = merged.replace("check_for_update_on_startup = false",
                                      "check_for_update_on_startup = false # owner comment")
        self.assertEqual(config.restore_startup_update_default_text(changed_value, record), changed_value)
        self.assertEqual(config.restore_startup_update_default_text(changed_line, record), changed_line)

    def test_startup_default_rejects_malformed_toml_and_non_boolean_values(self):
        for source in ('not = [valid', 'check_for_update_on_startup = "false"\n'):
            with self.subTest(source=source), self.assertRaises(config.CodexTuiConfigError):
                config.merge_startup_update_default_text(source)

    def test_invalid_input_is_not_written_or_backed_up(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fragment = Path(__file__).resolve().parents[1] / "adapters/codex/config/tui-statusline.toml"
            path = root / "config.toml"
            for invalid in ('[tui\nstatus_line = ["x"]\n', '[tui]\nstatus_line = 7\n'):
                path.write_text(invalid, encoding="utf-8")
                with self.assertRaises(config.CodexTuiConfigError):
                    config.apply_status_line(path, fragment)
                self.assertEqual(path.read_text(encoding="utf-8"), invalid)
                self.assertFalse(path.with_name(config.BACKUP_NAME).exists())

    def test_dry_run_and_backup_are_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fragment = Path(__file__).resolve().parents[1] / "adapters/codex/config/tui-statusline.toml"
            path = root / "config.toml"
            path.write_text('[tui]\nstatus_line = ["x"]\n', encoding="utf-8")
            original = path.read_bytes()
            result = config.apply_status_line(path, fragment, dry_run=True)
            self.assertTrue(result["changed"])
            self.assertEqual(path.read_bytes(), original)
            first = config.apply_status_line(path, fragment)
            self.assertTrue(first["changed"])
            self.assertTrue(first["startup_changed"])
            self.assertEqual(path.with_name(config.BACKUP_NAME).read_bytes(), original)
            after = path.read_bytes()
            self.assertIs(tomllib.loads(after.decode("utf-8"))["check_for_update_on_startup"], False)
            second = config.apply_status_line(path, fragment)
            self.assertFalse(second["changed"])
            self.assertEqual(path.read_bytes(), after)

    def test_symlink_target_is_updated_with_one_time_backup_and_idempotently(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fragment = Path(__file__).resolve().parents[1] / "adapters/codex/config/tui-statusline.toml"
            target = root / "shared" / "codex.toml"
            target.parent.mkdir()
            original = (b'model = "keep"\n[tui]\n'
                        b'status_line = ["project-name", "thread-id", "custom-item"]\n'
                        b'status_line_use_colors = false\n')
            target.write_bytes(original)
            target.chmod(0o640)
            path = root / "config.toml"
            link_target = os.path.relpath(target, path.parent)
            path.symlink_to(link_target)

            first = config.apply_status_line(path, fragment)
            self.assertTrue(first["changed"])
            self.assertTrue(path.is_symlink())
            self.assertEqual(os.readlink(path), link_target)
            self.assertEqual(path.with_name(config.BACKUP_NAME).read_bytes(), original)
            merged_bytes = target.read_bytes()
            merged = tomllib.loads(merged_bytes.decode("utf-8"))
            self.assertIs(merged["check_for_update_on_startup"], False)
            self.assertEqual(merged["model"], "keep")
            self.assertEqual(merged["tui"]["status_line"],
                             ["thread-id", "project-name", "custom-item"])
            self.assertIs(merged["tui"]["status_line_use_colors"], False)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)

            second = config.apply_status_line(path, fragment)
            self.assertFalse(second["changed"])
            self.assertEqual(target.read_bytes(), merged_bytes)
            self.assertTrue(path.is_symlink())
            self.assertEqual(os.readlink(path), link_target)

    def test_startup_type_conflict_does_not_discard_status_line_merge(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fragment = Path(__file__).resolve().parents[1] / "adapters/codex/config/tui-statusline.toml"
            path = root / "config.toml"
            path.write_text('check_for_update_on_startup = "mine"\n[tui]\nstatus_line = []\n',
                            encoding="utf-8")
            result = config.apply_status_line(path, fragment)
            parsed = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(parsed["check_for_update_on_startup"], "mine")
            self.assertEqual(parsed["tui"]["status_line"][0], "thread-id")
            self.assertIn("must be a boolean", result["startup_error"])

    def test_invalid_symlink_target_is_unchanged_and_not_backed_up(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fragment = Path(__file__).resolve().parents[1] / "adapters/codex/config/tui-statusline.toml"
            target = root / "shared.toml"
            original = b'[tui]\nstatus_line = 7\n'
            target.write_bytes(original)
            path = root / "config.toml"
            path.symlink_to(target)

            with self.assertRaises(config.CodexTuiConfigError):
                config.apply_status_line(path, fragment)
            self.assertTrue(path.is_symlink())
            self.assertEqual(os.readlink(path), str(target))
            self.assertEqual(target.read_bytes(), original)
            self.assertFalse(path.with_name(config.BACKUP_NAME).exists())

    def test_feasibility_is_exact_and_overrides_stay_unknown(self):
        self.assertEqual(config.status_line_capability(''), "incapable")
        self.assertEqual(config.status_line_capability('[tui]\nstatus_line = []\n'), "incapable")
        self.assertEqual(config.status_line_capability(
            '[tui]\nstatus_line = ["x", "thread-id"]\n'), "capable")
        # First is the current order; last is a not-yet-refreshed install.
        self.assertEqual(config.status_line_capability(
            '[tui]\nstatus_line = ["thread-id", "x"]\n'), "capable")
        self.assertEqual(config.status_line_capability(
            '[tui]\nstatus_line = ' + json.dumps(list(config.DEFAULT_STATUS_LINE)) + '\n'), "capable")
        self.assertEqual(config.status_line_capability(
            '[tui]\nstatus_line = ' + json.dumps(FORMER_DEFAULT) + '\n'), "capable")
        self.assertEqual(config.status_line_capability(
            '[tui]\nstatus_line = ["x", "thread-id", "y"]\n'), "incapable")
        self.assertEqual(config.status_line_capability('[tui]\nstatus_line = 7\n'), "unknown")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(config.status_line_capability_for_home(root), "incapable")
            (root / "config.toml").write_text('[tui]\nstatus_line = ["thread-id"]\n')
            self.assertEqual(config.status_line_capability_for_home(
                root, argv=["codex", "-c", 'tui.status_line=["x"]']), "unknown")
            self.assertEqual(config.status_line_capability_for_home(
                root, argv=["codex", "--profile", "custom"]), "unknown")


if __name__ == "__main__":
    unittest.main()
