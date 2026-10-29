from __future__ import annotations

import json
import re
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/install"))
sys.path.insert(0, str(ROOT / "utilities"))
import model_settings
import native_context
import runtime_activation
import safe_fs


class NativeContextTests(unittest.TestCase):
    def test_codex_1m_request_roundtrip_repairs_drift_without_touching_existing_rollout(self):
        self.seed_models(value="1000000")
        config = self.home / "config.toml"
        config.write_bytes(b'# keep comment\r\nmodel_context_window = 800000 # native\r\n')
        rollout = self.home / "sessions/existing.jsonl"
        rollout.parent.mkdir()
        recorded = b'{"payload":{"type":"token_count","info":{"model_context_window":760000}}}\n'
        rollout.write_bytes(recorded)
        for value, status in (("1000000", "unchanged"), ("800000", "changed"),
                              ("1000000", "changed")):
            result = model_settings.set_model("codex", "context-window", value,
                                              runtime=self.home, source_root=ROOT)
            self.assertEqual(result["status"], status)
            self.assertEqual(result["new_context_window"], value)
            self.assertEqual(model_settings.get_context_window(
                "codex", runtime=self.home, source_root=ROOT)["context_window"], value)
            self.assertEqual(config.read_bytes(), (
                f'# keep comment\r\nmodel_context_window = {value} # native\r\n').encode())
            self.assertEqual(rollout.read_bytes(), recorded)
        before = config.read_bytes()
        result = model_settings.set_model("codex", "context-window", "800000", dry_run=True,
                                          runtime=self.home, source_root=ROOT)
        self.assertEqual(result["status"], "planned")
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual(rollout.read_bytes(), recorded)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="native-context-")
        self.home = Path(self.temp.name)
        self.old_fixture = os.environ.get("HEARTING_FIXTURE_ROOT")
        os.environ["HEARTING_FIXTURE_ROOT"] = str(self.home)

    def seed_models(self, adapter="codex", value="760000"):
        config_dir = self.home / "agent-config"
        config_dir.mkdir(exist_ok=True)
        source = ROOT / "adapters" / adapter / "config/models.conf"
        raw = source.read_text(encoding="utf-8")
        import re
        raw, count = re.subn(r"^CFG_MODEL_CONTEXT_WINDOW=.*$",
                             f"CFG_MODEL_CONTEXT_WINDOW={value}", raw, count=1, flags=re.M)
        if not count:
            raw += f"\nCFG_MODEL_CONTEXT_WINDOW={value}\n"
        (config_dir / "models.conf").write_text(raw, encoding="utf-8")

    def tearDown(self):
        if self.old_fixture is None:
            os.environ.pop("HEARTING_FIXTURE_ROOT", None)
        else:
            os.environ["HEARTING_FIXTURE_ROOT"] = self.old_fixture
        self.temp.cleanup()

    def test_codex_root_value_changes_in_place_and_profile_is_reported(self):
        config = self.home / "config.toml"
        original = ("# 한글 사용자 주석\r\nprofile = \"x\"\r\nmodel_context_window = 800000 # managed\r\n"
                    "model = \"vendor/model\"\r\n\r\n[profiles.x]\r\nmodel_context_window = 900000\r\n")
        original = original.encode("utf-8")
        config.write_bytes(original)
        self.seed_models()
        result = native_context.project_context_window(
            "codex", runtime=self.home, source_root=ROOT)
        updated = config.read_bytes()
        self.assertIn(b"model_context_window = 760000 # managed\r\n", updated)
        self.assertIn(b"[profiles.x]\r\nmodel_context_window = 900000\r\n", updated)
        self.assertEqual(updated.replace(b"760000", b"800000"), original)
        self.assertEqual(updated.count(b"model_context_window = 760000"), 1)
        self.assertNotIn(b"default_mode_request_user_input", updated)
        self.assertTrue(any("profile" in line for line in result["report_lines"]))

    def test_codex_absent_baseline_restoration_keeps_trailing_comment_and_crlf(self):
        config = self.home / "config.toml"
        original = b"# keep\r\nmodel = \"x\"\r\n[profile.foo]\r\nmodel_context_window = 900000\r\n"
        config.write_bytes(original)
        self.seed_models()
        result = native_context.project_context_window("codex", runtime=self.home, source_root=ROOT)
        record = result["native_context"]["codex"]["keys"]["model_context_window"]
        self.assertEqual(record["baseline"], {"present": False})
        self.assertTrue(result["native_context"]["codex"]["file_created"] is False)
        projected = config.read_bytes()
        restored, changed = native_context.restore_codex_context(projected, record)
        self.assertTrue(changed)
        self.assertEqual(restored, original)

    def test_codex_baseline_value_restores_only_on_exact_match(self):
        record = {"baseline": {"present": True, "value": 640000}, "projected": 760000}
        before = b'model_context_window = 760000 # managed\r\nmodel = "x"\r\n'
        restored, changed = native_context.restore_codex_context(before, record)
        self.assertTrue(changed)
        self.assertEqual(restored, b'model_context_window = 640000 # managed\r\nmodel = "x"\r\n')
        edited, changed = native_context.restore_codex_context(
            before.replace(b"760000", b"810000"), record)
        self.assertFalse(changed)
        self.assertIn(b"810000", edited)
        malformed, changed = native_context.restore_codex_context(
            before, {"baseline": {"present": False}, "projected": True})
        self.assertFalse(changed)
        self.assertEqual(malformed, before)

    def test_codex_insert_precedes_table_and_ignores_multiline_string_header(self):
        config = self.home / "config.toml"
        config.write_text('note = """\n[not-a-table]\nmodel_context_window = 5\n"""\n\n[model]\nname="x"',
                          encoding="utf-8")
        self.seed_models()
        native_context.project_context_window("codex", runtime=self.home, source_root=ROOT)
        text = config.read_text(encoding="utf-8")
        self.assertEqual(text.count("model_context_window = 760000"), 1)
        self.assertLess(text.index("model_context_window = 760000"), text.index("[model]"))
        self.assertIn("[not-a-table]\nmodel_context_window = 5", text)

    def test_codex_insert_skips_escaped_triple_quote_inside_multiline_string(self):
        config = self.home / "config.toml"
        original = ('prompt = """\ntext \\"""\n[profiles.fake]\nstill string content\n"""\n'
                    '[profiles.real]\nmodel = "m"\n')
        config.write_text(original, encoding="utf-8")
        self.seed_models()
        native_context.project_context_window("codex", runtime=self.home, source_root=ROOT)
        text = config.read_text(encoding="utf-8")
        head, tail = original.split("[profiles.real]")
        self.assertEqual(text, head + "model_context_window = 760000\n[profiles.real]" + tail)
        parsed = native_context.tomllib.loads(text)
        self.assertEqual(parsed["model_context_window"], 760000)
        self.assertIn("[profiles.fake]", parsed["prompt"])

    def test_codex_one_line_strings_holding_triple_quotes_do_not_open_multiline(self):
        config = self.home / "config.toml"
        strings = 'a = "x \\"\\"\\" y"\nb = \'"""\'\nc = "\'\'\'"\n'
        config.write_text(strings + "[t]\nx = 1\n", encoding="utf-8")
        self.seed_models()
        native_context.project_context_window("codex", runtime=self.home, source_root=ROOT)
        self.assertEqual(config.read_text(encoding="utf-8"),
                         strings + "model_context_window = 760000\n[t]\nx = 1\n")
        config.write_text(strings + "model_context_window = 5\n[t]\nx = 1\n", encoding="utf-8")
        native_context.project_context_window("codex", runtime=self.home, source_root=ROOT)
        self.assertEqual(config.read_text(encoding="utf-8"),
                         strings + "model_context_window = 760000\n[t]\nx = 1\n")

    def test_codex_replaces_real_root_key_after_key_shaped_string_line(self):
        config = self.home / "config.toml"
        config.write_text('p = """\nmodel_context_window = 1\n"""\nq = \'\'\'\n'
                          'model_context_window = 2\n\'\'\'\nmodel_context_window = 3\n[t]\n',
                          encoding="utf-8")
        self.seed_models()
        native_context.project_context_window("codex", runtime=self.home, source_root=ROOT)
        self.assertEqual(config.read_text(encoding="utf-8"),
                         'p = """\nmodel_context_window = 1\n"""\nq = \'\'\'\n'
                         'model_context_window = 2\n\'\'\'\nmodel_context_window = 760000\n[t]\n')

    def test_codex_misplaced_edit_fails_closed_and_leaves_config(self):
        config = self.home / "config.toml"
        config.write_text('prompt = """\nbody\n"""\n[t]\n', encoding="utf-8")
        self.seed_models()
        before = config.read_bytes()
        # Simulate a scanner miss that would land the key inside the string.
        with patch.object(native_context, "_root_scan", return_value=(None, before.index(b"body"))):
            with self.assertRaisesRegex(native_context.NativeContextError, "root model_context_window"):
                native_context.project_context_window("codex", runtime=self.home, source_root=ROOT)
        self.assertEqual(config.read_bytes(), before)

    def test_activation_reports_projection_failure_as_activation_error(self):
        codex_home = self.home / ".codex"
        (codex_home / "agent-config").mkdir(parents=True)
        (codex_home / "agent-config/models.conf").write_text(
            (ROOT / "adapters/codex/config/models.conf").read_text())
        (codex_home / "config.toml").write_text("model_context_window = 800000\n[broken\n")
        env = {**os.environ, "HOME": str(self.home), "ZDOTDIR": str(self.home),
               "CODEX_HOME": str(codex_home), "HEARTING_FIXTURE_ROOT": str(self.home)}
        env.pop("HEARTING_MODEL_CONFIG_HOME", None)
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(runtime_activation.ActivationError, "invalid Codex config TOML"):
                runtime_activation._prepare_runtime_config("codex", ROOT, None)

    def test_claude_context_only_overwrites_managed_value_and_deactivate_keeps_it(self):
        settings = self.home / "settings.json"
        settings.write_text(json.dumps({"other": {"keep": 1}, "env": {
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "800000", "USER_KEY": "value"}},
            separators=(",", ":")), encoding="utf-8")
        self.seed_models("claude", "760000")
        result = native_context.project_context_window(
            "claude", runtime=self.home, source_root=ROOT,
            claude_merge=lambda **kwargs: runtime_activation._merge_claude_settings(
                ROOT, None, config_path=settings, **kwargs), context_only=True)
        data = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual(data["env"], {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "760000", "USER_KEY": "value"})
        self.assertTrue(result["changed"])
        runtime_activation._unmerge_claude_settings({"managed_config": {
            "claude_values": {"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "760000"}}}}, "global",
            config_path=settings)
        # The merge's teardown path deliberately excludes the operating preference.
        self.assertEqual(json.loads(settings.read_text())["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "760000")

    def test_claude_context_receipt_restores_prior_value_and_empty_container(self):
        settings = self.home / "settings.json"
        settings.write_text(json.dumps({"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "640000",
                                                  "USER_KEY": "keep"}}), encoding="utf-8")
        self.seed_models("claude", "760000")
        projected = native_context.project_context_window(
            "claude", runtime=self.home, source_root=ROOT,
            claude_merge=lambda **kwargs: runtime_activation._merge_claude_settings(
                ROOT, None, config_path=settings, **kwargs), context_only=True)
        record = projected["native_context"]["claude"]["keys"][
            "env.CLAUDE_CODE_AUTO_COMPACT_WINDOW"]
        self.assertEqual(record["baseline"], {"present": True, "value": "640000"})
        runtime_activation._unmerge_claude_settings(
            {"managed_config": {"native_context": projected["native_context"]}},
            "global", config_path=settings)
        self.assertEqual(json.loads(settings.read_text())["env"], {
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "640000", "USER_KEY": "keep"})

    def test_claude_uninstall_removes_activation_added_empty_env_but_keeps_original_empty_env(self):
        for had_env in (False, True):
            with self.subTest(had_env=had_env):
                settings = self.home / ("settings-%s.json" % had_env)
                original = {"env": {}} if had_env else {}
                settings.write_text(json.dumps(original), encoding="utf-8")
                projected = native_context._native_key_record(
                    None, "claude", False, None, "760000", extra={"env_present": had_env})
                data = {"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "760000"}}
                settings.write_text(json.dumps(data), encoding="utf-8")
                runtime_activation._unmerge_claude_settings(
                    {"managed_config": {"native_context": {"claude": {"keys": {
                        "env.CLAUDE_CODE_AUTO_COMPACT_WINDOW": projected}}}}},
                    "global", config_path=settings)
                actual = json.loads(settings.read_text(encoding="utf-8"))
                self.assertEqual(actual, {"env": {}} if had_env else {})

    def test_model_set_repairs_native_drift_even_when_scalar_is_unchanged(self):
        self.seed_models()
        result = model_settings.set_model("codex", "context-window", "760000",
                                          runtime=self.home, source_root=ROOT)
        self.assertEqual(result["status"], "unchanged")
        config = (self.home / "config.toml").read_text(encoding="utf-8")
        self.assertIn("model_context_window = 760000", config)

    def test_model_set_claude_updates_only_native_context_env(self):
        config_dir = self.home / ".claude/agent-config"
        config_dir.mkdir(parents=True)
        text = (ROOT / "adapters/claude/config/models.conf").read_text(encoding="utf-8")
        (config_dir / "models.conf").write_text(text.replace("CFG_MODEL_CONTEXT_WINDOW=800000",
                                                               "CFG_MODEL_CONTEXT_WINDOW=800000"))
        settings = self.home / ".claude/settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({"other": True, "env": {"USER_KEY": "keep",
                            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "800000"}}), encoding="utf-8")
        result = model_settings.set_model("claude", "context-window", "760000",
                                          runtime=self.home / ".claude", source_root=ROOT)
        self.assertEqual(result["status"], "changed")
        self.assertEqual(sum(" -> " in line for line in result["lines"]), 1)
        data = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual(data["env"], {"USER_KEY": "keep", "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "760000"})

    def test_model_set_projection_failure_reports_saved_scalar_and_retry_repairs_native(self):
        self.seed_models("codex", "800000")
        config = self.home / "config.toml"
        config.write_text("model_context_window = 800000\n[broken\n", encoding="utf-8")
        with self.assertRaisesRegex(model_settings.ModelSettingsError, "models.conf was saved"):
            model_settings.set_model("codex", "context-window", "760000",
                                     runtime=self.home, source_root=ROOT)
        selected = model_settings.model_config.parse_config(self.home / "agent-config/models.conf")
        self.assertEqual(selected["CFG_MODEL_CONTEXT_WINDOW"], "760000")
        config.write_text("model = \"vendor/model\"\n", encoding="utf-8")
        result = model_settings.set_model("codex", "context-window", "760000",
                                          runtime=self.home, source_root=ROOT)
        self.assertEqual(result["status"], "unchanged")
        self.assertIn("model_context_window = 760000", config.read_text(encoding="utf-8"))

    def test_activation_projects_both_native_preferences(self):
        claude_home, codex_home = self.home / ".claude", self.home / ".codex"
        claude_home.mkdir(); codex_home.mkdir()
        claude_config = claude_home / "agent-config"
        codex_config = codex_home / "agent-config"
        claude_config.mkdir(); codex_config.mkdir()
        claude_text = (ROOT / "adapters/claude/config/models.conf").read_text()
        (claude_config / "models.conf").write_text(claude_text.replace(
            "CFG_MODEL_CONTEXT_WINDOW=800000", "CFG_MODEL_CONTEXT_WINDOW=760000"))
        (codex_config / "models.conf").write_text(re.sub(
            r"(?m)^CFG_MODEL_CONTEXT_WINDOW=\d+$", "CFG_MODEL_CONTEXT_WINDOW=760000",
            (ROOT / "adapters/codex/config/models.conf").read_text(), count=1))
        env = {**os.environ, "HOME": str(self.home), "ZDOTDIR": str(self.home),
               "CLAUDE_CONFIG_DIR": str(claude_home), "CODEX_HOME": str(codex_home),
               "HEARTING_FIXTURE_ROOT": str(self.home)}
        env.pop("HEARTING_MODEL_CONFIG_HOME", None)
        with patch.dict(os.environ, env, clear=True):
            with safe_fs.TargetLocks((claude_home / "settings.json", codex_home / "config.toml")):
                claude = runtime_activation._prepare_runtime_config("claude", ROOT, None,
                                                                    native_locked=True)
                codex = runtime_activation._prepare_runtime_config("codex", ROOT, None,
                                                                   native_locked=True)
        cdata = json.loads((claude_home / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(cdata["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "760000")
        text = (codex_home / "config.toml").read_text(encoding="utf-8")
        self.assertIn("model_context_window = 760000", text)
        self.assertTrue(any("760000" in line for line in claude[0]["report_lines"]))
        self.assertTrue(any("760000" in line for line in codex[0]["report_lines"]))

    def test_claude_worker_home_receives_dynamic_context_env_and_permissions(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("build_home_fixture", ROOT / "tools/profile/build-home.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        source = self.home / "user/.claude"
        source.mkdir(parents=True)
        (source / "settings.json").write_text(json.dumps({"env": {
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "760000"}, "permissions": {"allow": ["Read"]}}))
        destination = self.home / "worker"
        env = {"HOME": str(self.home / "user"), "CLAUDE_CONFIG_DIR": str(source),
               "AGENT_DISPATCH_JOBS": str(self.home / "jobs.log"), "AGENT_HOME": str(ROOT)}
        module.build_worker_home(ROOT, "claude", "stage", "fixture-session",
                                 env=env, destination=destination)
        copied = json.loads((destination / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(copied["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "760000")
        self.assertEqual(copied["permissions"], {"allow": ["Read"]})

    def test_codex_bad_toml_is_left_untouched(self):
        config = self.home / "config.toml"
        config.write_text("model_context_window = 800000\n[broken\n", encoding="utf-8")
        self.seed_models()
        before = config.read_bytes()
        with self.assertRaises(native_context.NativeContextError):
            native_context.project_context_window("codex", runtime=self.home, source_root=ROOT)
        self.assertEqual(config.read_bytes(), before)

    def test_codex_non_positive_original_is_refused_before_projection(self):
        # Nothing is projected or recorded, so uninstall never has such a baseline to restore.
        config = self.home / "config.toml"
        self.seed_models()
        for original in (b"model_context_window = 0\n", b"model_context_window = -5\n"):
            config.write_bytes(original)
            with self.assertRaisesRegex(native_context.NativeContextError, "positive integer"):
                native_context.project_context_window("codex", runtime=self.home, source_root=ROOT)
            self.assertEqual(config.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
