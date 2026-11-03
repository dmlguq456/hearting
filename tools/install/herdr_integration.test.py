"""Pinned Herdr asset, user-config migration, and native registration fixtures."""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "utilities"))
import herdr_integration as herdr  # noqa: E402
import fixture_env  # noqa: E402
import projector  # noqa: E402
import runtime_activation as activation  # noqa: E402


class ManagedCopyVerifyTest(unittest.TestCase):
    """GA-A7-004: `verify` runs every check a driver lists; a managed copy's check is one too."""

    def test_each_runtime_lists_a_callable_managed_copy_check_that_runs(self):
        import verifier
        passing = lambda *_a, **_k: (lambda: {"id": "other", "ok": True, "detail": ""})
        for runtime in herdr.VERSIONS:
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as temporary:
                driver = importlib.import_module(f"drivers.{runtime}")
                dest = Path(temporary) / "managed-asset"
                entry = {"action": "copy_managed", "dest": str(dest), "integration_id": runtime,
                         "source": str(dest)}
                with mock.patch.object(driver.projector, "plan", return_value={runtime: [entry]}), \
                        mock.patch.object(driver.verifier, "check_herdr_integration", side_effect=passing), \
                        mock.patch.object(driver.verifier, "check_cmd", side_effect=passing):
                    checks = driver.checks()
                self.assertTrue(checks and all(callable(check) for check in checks))
                results = verifier.run(runtime, mock.Mock(checks=lambda: checks))
                managed = [row for row in results if row["id"] == f"{runtime}.herdr.installed.{runtime}"]
                self.assertEqual(len(managed), 1)
                self.assertFalse(managed[0]["ok"])          # absent copy: a failed check, not a TypeError


class PinnedSourceTest(unittest.TestCase):
    def test_three_runtime_sources_have_the_expected_marker_and_entrypoint(self):
        for runtime in herdr.VERSIONS:
            with self.subTest(runtime=runtime):
                checks = herdr.source_checks(runtime, ROOT)
                self.assertTrue(checks)
                self.assertFalse([item for item in checks if not item["ok"]], checks)

    def test_claude_reporter_has_one_copy_managed_destination(self):
        with tempfile.TemporaryDirectory(prefix="herdr-claude-plan-") as temporary:
            home = Path(temporary) / "claude-home"
            with mock.patch.object(projector.paths, "runtime_home", return_value=home):
                entries = projector.plan(["claude"], scope="global")["claude"]
        dest = home / "hooks/herdr-agent-state.sh"
        matching = [entry for entry in entries if Path(entry.get("dest", "")) == dest]
        self.assertEqual(len(matching), 1, matching)
        self.assertEqual(matching[0]["action"], "copy_managed")
        self.assertEqual(matching[0]["integration_id"], "claude")

    def test_claude_template_command_tracks_the_owned_herdr_command(self):
        settings = json.loads((ROOT / "adapters/claude/settings.json").read_text(encoding="utf-8"))
        commands = [hook.get("command") for groups in settings["hooks"].values()
                    for group in groups for hook in group.get("hooks", [])]
        self.assertIn(herdr.CLAUDE_SESSION_COMMAND, commands)
        self.assertTrue(herdr._owned_state_hook(
            herdr.CLAUDE_SESSION_COMMAND.replace(
                "${CLAUDE_CONFIG_DIR:-$HOME/.claude}", "$HOME/.claude"
            )
        ))
        self.assertTrue(herdr._owned_state_hook(herdr.CLAUDE_SESSION_COMMAND))

    def test_activation_respells_tuned_herdr_and_migrates_old_and_new_wrappers(self):
        old_root = "$HOME/.claude"
        new_root = "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
        old_direct = herdr.CLAUDE_SESSION_COMMAND.replace(new_root, old_root)
        old_wrapper = f'sh "{old_root}/hooks/run-hook.sh" bash herdr-agent-state.sh working'
        new_wrapper = f'sh "{new_root}/hooks/run-hook.sh" bash herdr-agent-state.sh idle'
        foreign = {"type": "command", "command": "/home/user/hooks/herdr-agent-state.sh working"}
        with tempfile.TemporaryDirectory(prefix="herdr-claude-alias-") as temporary:
            root = Path(temporary)
            active_root = root / "release"
            source_path = active_root / "adapters/claude/settings.json"
            source_path.parent.mkdir(parents=True)
            marker = active_root / "hooks/herdr-agent-state.sh"
            marker.parent.mkdir(parents=True)
            marker.write_text(
                "# HERDR_INTEGRATION_ID=claude\n# HERDR_INTEGRATION_VERSION=11\n",
                encoding="utf-8",
            )
            source = {
                "hooks": {"SessionStart": [{"matcher": "*", "hooks": [
                    {"type": "command", "command": herdr.CLAUDE_SESSION_COMMAND, "timeout": 10},
                ]}]},
                "statusLine": {"type": "command", "command": activation.CLAUDE_STATUSLINE_COMMAND},
                "autoMemoryEnabled": False,
                "env": {key: "" for key in activation.CLAUDE_MANAGED_ENV_KEYS},
            }
            source_path.write_text(json.dumps(source), encoding="utf-8")
            settings_path = root / "custom config" / "settings.json"
            settings_path.parent.mkdir(parents=True)
            tuned = {"type": "command", "command": old_direct, "timeout": 123, "async": True}
            original_settings = {
                **source,
                "hooks": {
                    "SessionStart": [{"matcher": "*", "hooks": [
                        tuned, {"type": "command", "command": "user-start"},
                    ]}],
                    "UserPromptSubmit": [{"matcher": "*", "hooks": [
                        {"type": "command", "command": old_wrapper},
                        {"type": "command", "command": new_wrapper},
                        foreign,
                    ]}],
                },
            }
            settings_path.write_text(json.dumps(original_settings), encoding="utf-8")
            with mock.patch.object(activation, "_config_path", return_value=settings_path):
                activation._merge_claude_settings(active_root, None)
            merged = json.loads(settings_path.read_text(encoding="utf-8"))
            start = merged["hooks"]["SessionStart"]
            self.assertEqual(start, [{"matcher": "*", "hooks": [
                {**tuned, "command": herdr.CLAUDE_SESSION_COMMAND},
                {"type": "command", "command": "user-start"},
            ]}])
            self.assertEqual(merged["hooks"]["UserPromptSubmit"], [{
                "matcher": "*", "hooks": [foreign]
            }])
            owned = [(event, hook["command"])
                     for event, groups in merged["hooks"].items()
                     for group in groups for hook in group.get("hooks", [])
                     if herdr._owned_state_hook(hook.get("command"))]
            self.assertEqual(owned, [("SessionStart", herdr.CLAUDE_SESSION_COMMAND)])
            after_first = settings_path.read_bytes()
            with mock.patch.object(activation, "_config_path", return_value=settings_path):
                activation._merge_claude_settings(active_root, None)
            self.assertEqual(settings_path.read_bytes(), after_first)

    def test_codex_uses_the_herdr_home_asset_for_callback_and_manifest(self):
        self.assertEqual(
            herdr.CODEX_SESSION_COMMAND,
            'bash "${CODEX_HOME:-$HOME/.codex}/herdr-agent-state.sh" session',
        )
        codex_asset = [item for item in projector._CODEX_TABLE
                       if item.get("action") == "copy_managed"
                       and item.get("integration_id") == "codex"]
        self.assertEqual(len(codex_asset), 1)
        self.assertEqual(codex_asset[0]["dest_name"], "herdr-agent-state.sh")
        self.assertNotIn(("herdr-agent-state.sh", "adapters/codex/hooks/herdr-agent-state.sh"),
                         projector._CODEX_FIXED_SYMLINKS)
        with tempfile.TemporaryDirectory(prefix="herdr-codex-manifest-") as temporary:
            root = Path(temporary)
            source = root / "source/adapters/codex/hooks/herdr-agent-state.sh"
            dest = root / "codex-home/herdr-agent-state.sh"
            source.parent.mkdir(parents=True)
            dest.parent.mkdir(parents=True)
            source.write_text("source\n", encoding="utf-8")
            dest.write_text("installed\n", encoding="utf-8")
            receipt = herdr.manifest_assets("codex", root / "source", root / "codex-home")
        self.assertEqual([item["path"] for item in receipt["assets"]],
                         ["herdr-agent-state.sh"])

    def test_configured_codex_session_command_parses_and_runs_in_fake_codex_home(self):
        hooks = json.loads((ROOT / "adapters/codex/hooks/hooks.json").read_text(encoding="utf-8"))
        commands = [item["command"] for group in hooks["hooks"]["SessionStart"]
                    for item in group.get("hooks", [])]
        self.assertIn(herdr.CODEX_SESSION_COMMAND, commands)
        with tempfile.TemporaryDirectory(prefix="codex session command ") as temporary:
            home = Path(temporary) / "codex home"
            home.mkdir()
            marker = Path(temporary) / "invoked.txt"
            helper = home / "herdr-agent-state.sh"
            helper.write_text('#!/bin/sh\nprintf "%s\\n" "$1" > "$CODEX_HOOK_MARKER"\n',
                              encoding="utf-8")
            helper.chmod(0o700)
            env = {**os.environ, "HOME": str(Path(temporary) / "home"),
                   "CODEX_HOME": str(home), "CODEX_HOOK_MARKER": str(marker)}
            env.pop("HERDR_SOCKET_PATH", None)
            env.pop("HERDR_PANE_ID", None)
            syntax = subprocess.run(["bash", "-n", "-c", herdr.CODEX_SESSION_COMMAND],
                                    text=True, capture_output=True, env=env, check=False)
            self.assertEqual(syntax.returncode, 0, syntax.stderr)
            result = subprocess.run(["bash", "-c", herdr.CODEX_SESSION_COMMAND],
                                    text=True, capture_output=True, env=env, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(marker.read_text(encoding="utf-8"), "session\n")

    def test_hearting_update_adopts_only_the_matching_herdr_owned_file(self):
        with tempfile.TemporaryDirectory(prefix="herdr-asset-adoption-") as temporary:
            root = Path(temporary)
            source = root / "release/adapters/codex/hooks/herdr-agent-state.sh"
            home = root / "codex-home"
            dest = home / "herdr-agent-state.sh"
            source.parent.mkdir(parents=True)
            home.mkdir()
            source.write_text("# installed by herdr\n# HERDR_INTEGRATION_ID=codex\n"
                              "# HERDR_INTEGRATION_VERSION=10\nnew\n", encoding="utf-8")
            dest.write_text("# installed by herdr\n# HERDR_INTEGRATION_ID=codex\n"
                            "# HERDR_INTEGRATION_VERSION=7\nold\n", encoding="utf-8")
            result = herdr.project_asset(source, dest, "codex", allowed_root=home)
            self.assertEqual(result["status"], "updated")
            self.assertFalse(dest.is_symlink())
            self.assertEqual(dest.read_bytes(), source.read_bytes())

            dest.write_text("user callback\n", encoding="utf-8")
            result = herdr.project_asset(source, dest, "codex", allowed_root=home)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(dest.read_text(encoding="utf-8"), "user callback\n")

    def test_user_hook_file_migrates_in_place_and_activation_receipt_unmerges_only_ours(self):
        with tempfile.TemporaryDirectory(prefix="herdr-claude-hooks-") as tmp:
            path = Path(tmp) / ".claude" / "settings.json"
            path.parent.mkdir()
            path.write_text(json.dumps({
                "env": {"USER_VALUE": "keep"},
                "statusLine": {"command": "user-status"},
                "hooks": {"UserPromptSubmit": [{"matcher": "*", "hooks": [
                    {"type": "command", "command": herdr.CLAUDE_SESSION_COMMAND.replace(
                        " session", " working")},
                    {"type": "command", "command": "user-prompt"},
                ]}]},
            }), encoding="utf-8")
            with fixture_env.patched_environment(tmp, ROOT):
                result = herdr.migrate_hook_file(path, "claude", source_root=ROOT)
            self.assertEqual(result["status"], "updated")
            self.assertEqual(result["managed_config"], {
                "event": "SessionStart", "command": herdr.CLAUDE_SESSION_COMMAND})
            migrated = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(migrated["env"], {"USER_VALUE": "keep"})
            self.assertEqual(migrated["statusLine"], {"command": "user-status"})
            self.assertEqual(migrated["hooks"]["UserPromptSubmit"][0]["hooks"], [
                {"type": "command", "command": "user-prompt"}])
            with fixture_env.patched_environment(tmp, ROOT):
                removed = herdr.remove_managed_hook_file(path, result["managed_config"])
            self.assertTrue(removed)
            restored = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("SessionStart", restored["hooks"])
            self.assertEqual(restored["hooks"]["UserPromptSubmit"][0]["hooks"], [
                {"type": "command", "command": "user-prompt"}])

    def test_missing_optional_asset_removes_old_writer_without_registering_a_new_one(self):
        with tempfile.TemporaryDirectory(prefix="herdr-old-source-") as tmp:
            root = Path(tmp) / "old-source"
            path = Path(tmp) / ".claude" / "settings.json"
            path.parent.mkdir()
            path.write_text(json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [
                {"type": "command", "command":
                 'sh "$HOME/.claude/hooks/run-hook.sh" bash herdr-agent-state.sh working'},
            ]}]}}), encoding="utf-8")
            with fixture_env.patched_environment(tmp, ROOT):
                result = herdr.migrate_hook_file(path, "claude", source_root=root)
            self.assertEqual(result["status"], "updated")
            self.assertIsNone(result["managed_config"])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"hooks": {}})

    def test_claude_and_codex_keep_one_session_reporter_and_user_hooks(self):
        cases = (
            ("claude", herdr.CLAUDE_SESSION_COMMAND),
            ("codex", herdr.CODEX_SESSION_COMMAND),
        )
        for runtime, expected in cases:
            if runtime == "claude":
                old_idle = 'sh "$HOME/.claude/hooks/run-hook.sh" bash herdr-agent-state.sh idle'
                old_working = 'sh "$HOME/.claude/hooks/run-hook.sh" bash herdr-agent-state.sh working'
                old_blocked = 'sh "$HOME/.claude/hooks/run-hook.sh" bash herdr-agent-state.sh blocked'
            else:
                old_idle = '"$root/adapters/codex/hooks/run-hook.sh" herdr-agent-state.sh idle'
                old_working = '"$root/adapters/codex/hooks/run-hook.sh" herdr-agent-state.sh working'
                old_blocked = '"$root/adapters/codex/hooks/run-hook.sh" herdr-agent-state.sh blocked'
            data = {
                "env": {"USER_KEY": "keep"},
                "hooks": {
                    "SessionStart": [{"matcher": "*", "hooks": [
                        {"type": "command", "command": "user-start"},
                        {"type": "command", "command": old_idle},
                    ]}],
                    "UserPromptSubmit": [{"matcher": "*", "hooks": [
                        {"type": "command", "command": old_working},
                        {"type": "command", "command": "/home/me/hooks/herdr-agent-state.sh working"},
                        {"type": "command", "command": "herdr-session-projection.py"},
                    ]}],
                    "PermissionRequest": [{"matcher": "*", "hooks": [
                        {"type": "command", "command": old_blocked},
                        {"type": "command", "command": "user-permission"},
                    ]}],
                },
            }
            with self.subTest(runtime=runtime):
                self.assertTrue(herdr.migrate_hook_data(data, runtime))
                self.assertFalse(herdr.migrate_hook_data(data, runtime))
                self.assertEqual(data["env"], {"USER_KEY": "keep"})
                commands = [(event, hook["command"])
                            for event, groups in data["hooks"].items()
                            for group in groups
                            for hook in group.get("hooks", [])]
                self.assertEqual([(event, command) for event, command in commands
                                  if herdr._owned_state_hook(command)],
                                 [("SessionStart", expected)])
                self.assertIn(("SessionStart", "user-start"), commands)
                self.assertIn(("UserPromptSubmit", "herdr-session-projection.py"), commands)
                self.assertIn(("UserPromptSubmit", "/home/me/hooks/herdr-agent-state.sh working"), commands)
                self.assertIn(("PermissionRequest", "user-permission"), commands)


class JsoncRegistrationTest(unittest.TestCase):
    def test_plugin_edit_preserves_comments_and_other_entries(self):
        text = '''{
  // keep this user comment
  "enabled": true,
  "plugin": [
    "./user-plugin.js", // keep the user plugin
  ],
}
'''
        merged = herdr.edit_plugin_list(text, add=(herdr.OPENCODE_TUI_V1,))
        data = herdr.parse_jsonc(merged)
        self.assertIn("keep this user comment", merged)
        self.assertIn("keep the user plugin", merged)
        self.assertEqual(data["plugin"], ["./user-plugin.js", herdr.OPENCODE_TUI_V1])
        restored = herdr.edit_plugin_list(merged, remove=(herdr.OPENCODE_TUI_V1,))
        self.assertEqual(herdr.parse_jsonc(restored)["plugin"], ["./user-plugin.js"])
        self.assertIn("keep this user comment", restored)

    def test_migration_merges_both_versions_and_deactivation_keeps_user_config(self):
        with tempfile.TemporaryDirectory(prefix="herdr-config-") as tmp:
            home = Path(tmp) / "opencode"
            home.mkdir()
            tui = home / "tui.jsonc"
            tui.write_text('''{
  // user's existing TUI choices
  "plugin": ["./user-tui.js",],
  "theme": "user-theme",
}
''', encoding="utf-8")
            with fixture_env.patched_environment(tmp, ROOT):
                result = herdr.migrate_opencode_configs(home, ROOT)
            self.assertTrue(all(item["status"] in {"updated", "unchanged"}
                                for item in result["actions"]), result["actions"])
            merged_tui = herdr.parse_jsonc(tui.read_text(encoding="utf-8"))
            self.assertEqual(merged_tui["theme"], "user-theme")
            self.assertIn("./user-tui.js", merged_tui["plugin"])
            self.assertIn(herdr.OPENCODE_TUI_V1, merged_tui["plugin"])
            self.assertIn(herdr.OPENCODE_IDENTITY, merged_tui["plugin"])
            self.assertIn("user's existing TUI choices", tui.read_text(encoding="utf-8"))
            cli = home / "cli.json"
            self.assertTrue(cli.is_file())
            cli_data = json.loads(cli.read_text(encoding="utf-8"))
            self.assertIn(herdr.OPENCODE_TUI_V2, cli_data["plugins"])
            self.assertIn(herdr.OPENCODE_IDENTITY, cli_data["plugins"])
            refreshed = herdr.migrate_opencode_configs(
                home, ROOT, previous=result["managed_config"])
            self.assertFalse([item for item in refreshed["actions"] if item["changed"]],
                             refreshed["actions"])
            self.assertEqual(refreshed["managed_config"], result["managed_config"])
            with fixture_env.patched_environment(tmp, ROOT):
                removed = herdr.remove_opencode_configs(home, refreshed["managed_config"])
            self.assertIn(str(tui), removed)
            self.assertIn(str(cli), removed)
            restored = herdr.parse_jsonc(tui.read_text(encoding="utf-8"))
            self.assertEqual(restored["plugin"], ["./user-tui.js"])
            self.assertEqual(restored["theme"], "user-theme")
            self.assertFalse(cli.exists())

    def test_migration_repairs_both_json_formats_without_removing_user_or_identity_plugins(self):
        with tempfile.TemporaryDirectory(prefix="herdr config spaces ") as tmp:
            root = Path(tmp)
            home = root / ".config" / "opencode"
            home.mkdir(parents=True)
            configs = {
                "tui.jsonc": ("plugin", ["./user-tui.js", "./tui/herdr-tui-session.js",
                                           "../../old release/adapters/opencode/tui/herdr-tui-session.js",
                                           "./tui/hearting-tui-identity.ts"]),
                "tui.json": ("plugin", [str(root / "old release/adapters/opencode/tui/herdr-tui-session.js"),
                                          "./user-json.js", "./tui/hearting-tui-identity.ts"]),
                "cli.jsonc": ("plugins", ["./user-cli.js", "./tui/herdr-opencode",
                                             "../../old release/adapters/opencode/tui/herdr-opencode/tui.js",
                                             "./tui/hearting-tui-identity.ts"]),
                "cli.json": ("plugins", [str(home / "tui/herdr-opencode/tui.js"),
                                           "./user-cli-json.js", "./tui/hearting-tui-identity.ts"]),
            }
            for name, (key, values) in configs.items():
                encoded = json.dumps(values)
                (home / name).write_text(
                    f'{{\n  // preserve {name} user options\n  "{key}": {encoded},\n  "theme": "keep",\n}}\n',
                    encoding="utf-8")

            result = herdr.migrate_opencode_configs(home, ROOT)
            self.assertTrue(all(item["status"] in {"updated", "unchanged"}
                                for item in result["actions"]), result["actions"])
            for name, (key, original) in configs.items():
                text = (home / name).read_text(encoding="utf-8")
                data = herdr.parse_jsonc(text)
                expected = herdr.OPENCODE_TUI_V1 if key == "plugin" else herdr.OPENCODE_TUI_V2
                self.assertEqual(data[key].count(expected), 1, (name, data[key]))
                self.assertIn(herdr.OPENCODE_IDENTITY, data[key], name)
                self.assertIn(next(value for value in original if value.startswith("./user")),
                              data[key], name)
                self.assertEqual(data["theme"], "keep", name)
                self.assertIn(f"preserve {name} user options", text)
                self.assertFalse(any(herdr._legacy_opencode_tui_ref(
                    value, "tui" if key == "plugin" else "cli", home, ROOT)
                    for value in data[key]), (name, data[key]))

            rerun = herdr.migrate_opencode_configs(home, ROOT)
            self.assertFalse([item for item in rerun["actions"] if item["changed"]], rerun["actions"])

    def test_symlinked_runtime_config_is_never_rewritten(self):
        with tempfile.TemporaryDirectory(prefix="herdr-symlink-") as tmp:
            home = Path(tmp) / "opencode"
            home.mkdir()
            target = Path(tmp) / "external-tui.json"
            original = '{"plugin": ["./user.js"]}\n'
            target.write_text(original, encoding="utf-8")
            (home / "tui.json").symlink_to(target)
            result = herdr.migrate_opencode_configs(home, ROOT)
            self.assertEqual((home / "tui.json").read_text(encoding="utf-8"), original)
            self.assertEqual(result["actions"][0]["status"], "source-managed")


class NativeSessionReporterFixtureTest(unittest.TestCase):
    def _run_hook(self, harness, payload, *, allowed, launch_argv, extra_env=None,
                  expect_report=True, source_asset=False, stale_tree=False):
        with tempfile.TemporaryDirectory(prefix="herdr session hook ") as tmp:
            root = Path(tmp)
            tools = root / "tools" / "fleet"
            tools.mkdir(parents=True)
            (tools / "__init__.py").write_text("", encoding="utf-8")
            (tools / "herdr_projection.py").write_text(
                "import os\n"
                "def may_report(harness, session_id, pane_id=None):\n"
                "    return (os.environ.get('REPORTER_FIXTURE_ALLOW') == '1' and "
                "os.environ.get('REPORTER_FIXTURE_SESSION') == session_id and "
                "pane_id == 'pane-fixture' and "
                "os.environ.get('AGENT_SESSION_ROLE') != 'worker' and "
                "os.environ.get('AGENT_DISPATCH_CHILD') != '1' and "
                "not os.environ.get('AGENT_DISPATCH_DEPTH'))\n",
                encoding="utf-8",
            )
            shutil.copyfile(ROOT / "tools/fleet/process_identity.py", tools / "process_identity.py")
            with (tools / "process_identity.py").open("a", encoding="utf-8") as module:
                module.write("\nimport json, os\n"
                             "def current_launch_context(harness, session_id):\n"
                             "    if (os.environ.get('REPORTER_LAUNCH_HARNESS') != harness or "
                             "os.environ.get('REPORTER_FIXTURE_SESSION') != session_id): return None\n"
                             "    return {'argv': json.loads(os.environ['REPORTER_LAUNCH_ARGV'])}\n")
            if harness == "claude":
                source = ROOT / "hooks" / "herdr-agent-state.sh"
                script = source if source_asset else root / ".claude" / "hooks" / "herdr-agent-state.sh"
            else:
                source = ROOT / "adapters" / "codex" / "hooks" / "herdr-agent-state.sh"
                script = source if source_asset else root / "codex home" / "herdr-agent-state.sh"
            if stale_tree:
                # A nearer tree whose package imports but whose projection fails.
                stale = root / "stale"
                (stale / "tools" / "fleet").mkdir(parents=True)
                (stale / "tools" / "fleet" / "__init__.py").write_text("", encoding="utf-8")
                (stale / "tools" / "fleet" / "herdr_projection.py").write_text(
                    "raise ImportError('stale tree')\n", encoding="utf-8")
                script = stale / "hooks" / "herdr-agent-state.sh"
                # Codex tries AGENT_HOME first: point it at the broken tree.
                extra_env = {**(extra_env or {}), **({"AGENT_HOME": str(stale)} if harness == "codex" else {})}
            if not source_asset:
                script.parent.mkdir(parents=True)
                shutil.copyfile(source, script)

            socket_path = str(root / "herdr.sock")
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(socket_path)
            listener.listen(1)
            listener.settimeout(1)
            received = []

            def serve_once():
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    return
                with connection:
                    with connection.makefile("rb") as stream:
                        line = stream.readline()
                    received.append(json.loads(line))
                    connection.sendall(b'{"result":{}}\n')

            server = None
            if allowed:
                server = threading.Thread(target=serve_once, daemon=True)
                server.start()
            env = {
                **fixture_env.build_environment(root, ROOT),
                "AGENT_HOME": str(root),
                "CODEX_HOME": str(root / "codex home"),
                "HERDR_ENV": "1",
                "HERDR_SOCKET_PATH": socket_path,
                "HERDR_PANE_ID": "pane-fixture",
                "REPORTER_FIXTURE_ALLOW": "1" if allowed else "0",
                "REPORTER_FIXTURE_SESSION": payload["session_id"],
                "REPORTER_LAUNCH_HARNESS": harness,
                "REPORTER_LAUNCH_ARGV": json.dumps(launch_argv),
            }
            for marker in ("AGENT_SESSION_ROLE", "AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_DEPTH",
                           "OPENCODE_DISPATCH_SLUG", "FLEET_TITLE_REFRESH", "MEM_DISTILL"):
                env.pop(marker, None)
            if harness == "codex":
                env["CODEX_THREAD_ID"] = payload["session_id"]
            env.update(extra_env or {})
            result = subprocess.run(["sh", str(script), "session"], input=json.dumps(payload),
                                    text=True, capture_output=True, env=env, timeout=5, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            if server is not None:
                server.join(2)
                self.assertFalse(server.is_alive(), "native hook did not connect to Herdr")
                self.assertEqual(bool(received), expect_report)
            else:
                with self.assertRaises(socket.timeout):
                    listener.accept()
            listener.close()
            return received[0] if received else None

    def test_a_broken_candidate_tree_falls_through_to_the_next_one(self):
        for harness, launch in (("claude", ["claude", "--resume", "ses-A"]),
                                ("codex", ["codex", "resume", "ses-A"])):
            with self.subTest(harness=harness):
                request = self._run_hook(harness, {
                    "hook_event_name": "SessionStart", "session_id": "ses-A", "source": "startup",
                    "transcript_path": str(Path(tempfile.gettempdir()) / "ses-A.jsonl"),
                }, allowed=True, launch_argv=launch, stale_tree=True)
                self.assertEqual(request["params"]["agent_session_id"], "ses-A")

    def test_native_hooks_forward_transcript_source_resume_argv_and_nanosecond_sequence(self):
        for harness, launch in (("claude", ["claude", "--resume", "ses-A"]),
                                ("codex", ["codex", "resume", "ses-A"])):
            for source in ("startup", "resume", "clear", "compact", "fork"):
                with self.subTest(harness=harness, source=source):
                    request = self._run_hook(harness, {
                        "hook_event_name": "SessionStart", "session_id": "ses-B",
                        "transcript_path": "/fixture/transcript.jsonl", "source": source,
                    }, allowed=True, launch_argv=launch,
                        extra_env={"REPORTER_FIXTURE_SESSION": "ses-B"})
                    self.assertEqual(request["method"], "pane.report_agent_session")
                    params = request["params"]
                    self.assertEqual(params["agent"], harness)
                    self.assertEqual(params["agent_session_id"], "ses-B")
                    self.assertEqual(params["agent_session_path"], "/fixture/transcript.jsonl")
                    self.assertEqual(params["session_start_source"], source)
                    expected = [harness, "--resume", "ses-B"] if harness == "claude" else [harness, "resume", "ses-B"]
                    self.assertEqual(params["resume_argv"], expected)
                    self.assertGreater(params["seq"], 10**18)

    def test_codex_source_and_runtime_copy_report_through_the_pinned_root_with_spaces(self):
        payload = {
            "hook_event_name": "SessionStart", "session_id": "ses-A",
            "transcript_path": "/fixture/transcript.jsonl", "source": "resume",
        }
        launch = ["codex", "resume", "ses-A"]
        for source_asset in (True, False):
            with self.subTest(source_asset=source_asset):
                request = self._run_hook("codex", payload, allowed=True, launch_argv=launch,
                                         source_asset=source_asset)
                self.assertEqual(request["method"], "pane.report_agent_session")
                self.assertEqual(request["params"]["agent_session_id"], "ses-A")

    def test_native_hooks_translate_proven_absolute_launcher_argv_for_wire(self):
        launches = {
            "claude": ["/home/user/.local/bin/claude", "--resume", "ses-A",
                       "--model", "fixture", "--setting-sources", "user"],
            "codex": ["/home/user/.local/bin/codex", "resume", "ses-A",
                      "-c", "k=v", "--model", "fixture"],
        }
        # Options outside each harness's known arity table are unsafe to replay,
        # so they collapse to the canonical resume form instead of being dropped.
        unsupported = {
            "claude": ["/home/user/.local/bin/claude", "--resume", "ses-A", "--color", "always"],
            "codex": ["/home/user/.local/bin/codex", "resume", "ses-A", "--setting-sources", "user"],
        }
        for harness, launch in launches.items():
            with self.subTest(harness=harness, case="supported-options"):
                request = self._run_hook(harness, {
                    "hook_event_name": "SessionStart", "session_id": "ses-A",
                    "transcript_path": "/fixture/transcript.jsonl", "source": "resume",
                }, allowed=True, launch_argv=launch)
                params = request["params"]
                self.assertEqual(params["resume_argv"], [harness, *launch[1:]])
                self.assertEqual(params["agent_session_id"], "ses-A")
                self.assertEqual(params["agent_session_path"], "/fixture/transcript.jsonl")
                self.assertEqual(params["session_start_source"], "resume")
                self.assertGreater(params["seq"], 10**18)

            with self.subTest(harness=harness, case="unsupported-option"):
                request = self._run_hook(harness, {
                    "hook_event_name": "SessionStart", "session_id": "ses-A",
                    "transcript_path": "/fixture/transcript.jsonl", "source": "resume",
                }, allowed=True, launch_argv=unsupported[harness])
                self.assertEqual(request["params"]["resume_argv"],
                                 [harness, "--resume", "ses-A"] if harness == "claude"
                                 else [harness, "resume", "ses-A"])
                self.assertEqual(request["params"]["agent_session_id"], "ses-A")

            for bad_argv in (["/usr/bin/env", harness, "--resume", "ses-A"],
                             ["/usr/bin/python3", f"/opt/{harness}", "--resume", "ses-A"],
                             [f"bin/{harness}", "--resume", "ses-A"],
                             [f"/opt/{harness}", "--resume", "ses-A", "\0bad"],
                             [f"/opt/{harness}", "--resume", "ses-A", "x" * 8192]):
                with self.subTest(harness=harness, argv=bad_argv[:3]):
                    request = self._run_hook(harness, {
                        "hook_event_name": "SessionStart", "session_id": "ses-A",
                        "transcript_path": "/fixture/transcript.jsonl", "source": "resume",
                    }, allowed=True, launch_argv=bad_argv)
                    self.assertNotIn("resume_argv", request["params"])
                    self.assertEqual(request["params"]["agent_session_id"], "ses-A")
                    self.assertEqual(request["params"]["agent_session_path"], "/fixture/transcript.jsonl")
                    self.assertEqual(request["params"]["session_start_source"], "resume")

    def test_native_session_reporters_refuse_worker_and_mismatched_codex_identity(self):
        for harness, launch in (("claude", ["claude", "--resume", "ses-A"]),
                                ("codex", ["codex", "resume", "ses-A"])):
            with self.subTest(harness=harness, case="worker"):
                self.assertIsNone(self._run_hook(harness, {
                    "hook_event_name": "SessionStart", "session_id": "ses-A",
                    "transcript_path": "/fixture/transcript.jsonl", "source": "resume",
                }, allowed=True, launch_argv=launch,
                    extra_env={"AGENT_SESSION_ROLE": "worker"}, expect_report=False))
        self.assertIsNone(self._run_hook("codex", {
            "hook_event_name": "SessionStart", "session_id": "ses-A",
            "transcript_path": "/fixture/transcript.jsonl", "source": "resume",
        }, allowed=True, launch_argv=["codex", "resume", "ses-A"],
            extra_env={"CODEX_THREAD_ID": "other-session"}, expect_report=False))

    def test_native_hooks_respect_report_guard_and_omit_unproven_resume_argv(self):
        for harness, launch in (("claude", ["claude", "--resume", "ses-A"]),
                                ("codex", ["codex", "resume", "ses-A"])):
            with self.subTest(harness=harness, case="non-resume"):
                request = self._run_hook(harness, {
                    "hook_event_name": "SessionStart", "session_id": "ses-A",
                    "transcript_path": "/fixture/transcript.jsonl", "source": "startup",
                }, allowed=True, launch_argv=launch)
                self.assertEqual(request["params"]["session_start_source"], "startup")
                self.assertEqual(request["params"]["resume_argv"], launch)
            with self.subTest(harness=harness, case="unsupported-source-shape"):
                request = self._run_hook(harness, {
                    "hook_event_name": "SessionStart", "session_id": "ses-A",
                    "transcript_path": "/fixture/transcript.jsonl", "source": {"unknown": True},
                }, allowed=True, launch_argv=launch)
                self.assertNotIn("session_start_source", request["params"])
                self.assertNotIn("resume_argv", request["params"])
            with self.subTest(harness=harness, case="guard-refused"):
                request = self._run_hook(harness, {
                    "hook_event_name": "SessionStart", "session_id": "ses-A",
                    "transcript_path": "/fixture/transcript.jsonl", "source": "resume",
                }, allowed=False, launch_argv=launch)
                self.assertIsNone(request)

    def test_codex_display_projection_carries_native_lifecycle_to_reporter(self):
        path = ROOT / "adapters" / "codex" / "hooks" / "herdr_session_projection.py"
        module_spec = importlib.util.spec_from_file_location("herdr_session_projection_fixture", path)
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        with mock.patch.object(module, "_project", return_value=True) as project, \
                mock.patch("peer_obligations.request_obligation_recovery"), \
                mock.patch.object(module.time, "time_ns", return_value=123456):
            module.project({"hook_event_name": "SessionStart", "source": "resume"}, "ses-A")
        project.assert_called_once_with("codex", "ses-A", worker=False, report_session=True,
                                        session_seq=123456, session_start_source="resume")


@unittest.skipUnless(shutil.which("node"), "Node.js is required for the asset registration fixture")
class PythonToNodeRegistrationTest(unittest.TestCase):
    def test_native_assets_and_v1_v2_registrations_load_together(self):
        script = r'''import fs from "node:fs";
import { pathToFileURL } from "node:url";
const root = process.env.HEARTING_TEST_ROOT;
const assets = process.env.HEARTING_TEST_ASSET_ROOT;
const read = (name) => JSON.parse(fs.readFileSync(`${root}/${name}`, "utf8"));
const claude = read("adapters/claude/settings.json");
const codex = read("adapters/codex/hooks/hooks.json");
const v1 = read("adapters/opencode/tui/tui.json");
const v2 = read("adapters/opencode/tui/cli.json");
const heartingPublisher = fs.readFileSync(`${root}/adapters/opencode/plugins/hearting-guards.js`, "utf8");
const commands = (hooks) => Object.values(hooks).flat().flatMap((group) => group.hooks || [])
  .map((hook) => hook.command || "");
if (commands(claude.hooks).filter((value) => value.includes("herdr-agent-state.sh")).length !== 1) throw Error("Claude reporter registration");
if (commands(codex.hooks).filter((value) => value.includes("herdr-agent-state.sh")).length !== 1) throw Error("Codex reporter registration");
if (!v1.plugin.includes("./herdr-tui-session.js")) throw Error("V1 TUI registration");
if (!v2.plugins.includes("./herdr-opencode")) throw Error("V2 CLI registration");
if (!heartingPublisher.includes("--no-report-session") || heartingPublisher.includes("paneReportSequence")) throw Error("duplicate OpenCode session reporter");
const backend = await import(pathToFileURL(`${assets}/plugins/herdr-agent-state.js`));
const tuiV1 = await import(pathToFileURL(`${assets}/herdr-tui-session.js`));
const tuiV2 = await import(pathToFileURL(`${assets}/herdr-opencode/tui.js`));
if (backend.default.id !== "herdr.opencode") throw Error("OpenCode backend export");
if (tuiV1.default.id !== "herdr.opencode.session-selection") throw Error("V1 TUI export");
if (tuiV2.default.id !== tuiV1.default.id) throw Error("V2 TUI export");
console.log("Claude/Codex session hooks and OpenCode v1/v2 assets registered");
'''
        with tempfile.TemporaryDirectory(prefix="herdr-node-assets-") as temporary:
            asset_root = Path(temporary)
            (asset_root / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
            for relative in (
                "adapters/opencode/plugins/herdr-agent-state.js",
                "adapters/opencode/tui/herdr-tui-session.js",
                "adapters/opencode/tui/herdr-opencode/tui.js",
            ):
                source_relative = relative.removeprefix("adapters/opencode/")
                target_relative = {
                    "plugins/herdr-agent-state.js": "plugins/herdr-agent-state.js",
                    "tui/herdr-tui-session.js": "herdr-tui-session.js",
                    "tui/herdr-opencode/tui.js": "herdr-opencode/tui.js",
                }[source_relative]
                target = asset_root / target_relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, target)
            result = subprocess.run(
                ["node", "--input-type=module", "-e", script],
                cwd=ROOT,
                env={**fixture_env.build_environment(asset_root, ROOT),
                     "HEARTING_TEST_ROOT": str(ROOT),
                     "HEARTING_TEST_ASSET_ROOT": str(asset_root)},
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("registered", result.stdout)

    def test_opencode_state_plugin_canonicalizes_only_proven_native_resume_argv(self):
        script = r'''import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import { pathToFileURL } from "node:url";
const base = process.env.HEARTING_TEST_ASSET_ROOT;
const backend = await import(pathToFileURL(`${base}/plugins/herdr-agent-state.js`));
const sid = "B";
const stat = fs.readFileSync("/proc/self/stat", "utf8");
const close = stat.lastIndexOf(") ");
const start = stat.slice(close + 2).trim().split(/\s+/)[19];
async function capture(index, identitySid, argv, identityStart = start, startSource = "clear") {
  const state = `${base}/state-${index}`;
  const identityDir = `${state}/hearting/tui-identity`;
  fs.mkdirSync(identityDir, {recursive: true});
  fs.writeFileSync(`${identityDir}/${process.pid}-${start}.json`, JSON.stringify({
    schema: "hearting-tui-selection-v1", sessionID: identitySid, pid: process.pid,
    start: identityStart, sessionSeq: 1700000000123, sessionStartSource: startSource,
  }));
  const socketPath = `${base}/plugin-${index}.sock`;
  process.env.HERDR_ENV = "1";
  process.env.HERDR_SOCKET_PATH = socketPath;
  process.env.HERDR_PANE_ID = "pane-fixture";
  process.env.XDG_STATE_HOME = state;
  process.argv = argv;
  let settle;
  const received = new Promise((resolve) => { settle = resolve; });
  const server = net.createServer((client) => {
    let buffer = "";
    client.on("data", (chunk) => {
      buffer += chunk;
      const newline = buffer.indexOf("\n");
      if (newline < 0) return;
      const request = JSON.parse(buffer.slice(0, newline));
      client.write("{\"result\":{}}\n");
      if (request.method === "pane.report_agent_session") settle(request);
    });
  });
  await new Promise((resolve) => server.listen(socketPath, resolve));
  const plugin = await backend.HerdrAgentStatePlugin();
  await plugin.event({event: {type: "session.updated", properties: {
    sessionID: sid, info: {id: sid, parentID: null},
  }}});
  let timeout;
  const request = await Promise.race([received, new Promise((_, reject) =>
    { timeout = setTimeout(() => reject(new Error("session report timeout")), 2500); })]);
  clearTimeout(timeout);
  await new Promise((resolve) => server.close(resolve));
  return {params: request.params, processArgv: process.argv.slice()};
}
const validArgv = ["/home/user/.local/bin/opencode", "/opt/opencode-cli.js", "run",
  "--session", "A", "--model", "fixture"];
const valid = await capture(1, sid, validArgv);
const expectedWireArgv = ["opencode", "--session", sid];
if (JSON.stringify(valid.params.resume_argv) !== JSON.stringify(expectedWireArgv)) throw Error("native wire argv was not canonicalized");
if (JSON.stringify(valid.processArgv) !== JSON.stringify(validArgv)) throw Error("raw process argv was mutated");
if (valid.params.agent_session_id !== sid || valid.params.session_start_source !== "clear" || valid.params.seq !== 1700000000123) {
  throw Error("native session metadata changed");
}
const rejected = [
  [sid, ["/usr/bin/env", "opencode", "run", "--session", sid]],
  [sid, ["/usr/bin/python3", "/opt/opencode", "run", "--session", sid]],
  [sid, ["bin/opencode", "/opt/opencode-cli.js", "run", "--session", sid]],
  [sid, ["/opt/opencode", "/opt/opencode-cli.js", "run", "--session"]],
  [sid, ["/opt/opencode", "/opt/opencode-cli.js", "run", "--session", sid, "\0bad"]],
  [sid, ["/opt/opencode", "/opt/opencode-cli.js", "run", "--session", sid, "x".repeat(8192)]],
  [sid, ["/opt/opencode", "/opt/opencode-cli.js", "run", ...Array(63).fill("x"), "--session", sid]],
];
for (let index = 0; index < rejected.length; index += 1) {
  const [identitySid, argv] = rejected[index];
  const params = await capture(index + 2, identitySid, argv);
  if ("resume_argv" in params.params) throw Error(`unproven launcher argv accepted at ${index}`);
  if (params.params.agent_session_id !== sid || params.params.session_start_source !== "clear"
      || params.params.seq !== 1700000000123) throw Error(`optional argv erased session identity at ${index}`);
}
const mismatched = await capture(20, "ses-other", validArgv);
if ("resume_argv" in mismatched.params || "seq" in mismatched.params || "session_start_source" in mismatched.params) {
  throw Error("current selection mismatch was accepted");
}
const changedBirth = await capture(21, sid, validArgv, "0");
if ("resume_argv" in changedBirth.params || "seq" in changedBirth.params || "session_start_source" in changedBirth.params) {
  throw Error("changed PID birth was accepted");
}
console.log("OpenCode state plugin canonicalized bounded proven native argv");
'''
        with tempfile.TemporaryDirectory(prefix="herdr-opencode-plugin-") as temporary:
            asset_root = Path(temporary)
            (asset_root / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
            plugins = asset_root / "plugins"
            plugins.mkdir()
            shutil.copyfile(ROOT / "adapters/opencode/plugins/herdr-agent-state.js",
                            plugins / "herdr-agent-state.js")
            result = subprocess.run(
                ["node", "--input-type=module", "-e", script], cwd=ROOT,
                env={**fixture_env.build_environment(asset_root, ROOT),
                     "HEARTING_TEST_ASSET_ROOT": str(asset_root),
                     "HEARTING_TEST_CASES": str(ROOT / "tools/fleet/tests/native_resume_cases.json")},
                text=True, capture_output=True, timeout=30, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("canonicalized bounded proven native argv", result.stdout)

    def test_opencode_tui_report_uses_pid_birth_bound_lifecycle_and_omits_unproven_argv(self):
        script = r'''import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import { pathToFileURL } from "node:url";
const base = process.env.HEARTING_TEST_ASSET_ROOT;
const tuiV1 = await import(pathToFileURL(`${base}/herdr-tui-session.js`));
const tuiV2 = await import(pathToFileURL(`${base}/herdr-opencode/tui.js`));
const sid = "B";
const cases = JSON.parse(fs.readFileSync(process.env.HEARTING_TEST_CASES, "utf8"))
  .filter((item) => item.harness === "opencode");
// The backend plugin only runs for `opencode run`/`--mini`, so it cannot be
// driven with the whole shared table. Keep its decision function textually
// identical to the TUI one, then check the table through the TUI path.
function nativeResumeFunction(file) {
  const text = fs.readFileSync(file, "utf8");
  const marker = "function nativeResumeWireArgv(";
  const startAt = text.indexOf(marker);
  if (startAt < 0) throw Error(`nativeResumeWireArgv missing from ${file}`);
  let depth = 0;
  for (let index = text.indexOf("{", startAt); index < text.length; index += 1) {
    if (text[index] === "{") depth += 1;
    else if (text[index] === "}") {
      depth -= 1;
      if (depth === 0) return text.slice(startAt, index + 1);
    }
  }
  throw Error(`nativeResumeWireArgv is unterminated in ${file}`);
}
const root = process.env.HEARTING_TEST_ROOT;
if (nativeResumeFunction(`${root}/adapters/opencode/plugins/herdr-agent-state.js`)
    !== nativeResumeFunction(`${base}/herdr-tui-session.js`)) {
  throw Error("OpenCode resume decision functions diverged");
}
const stat = fs.readFileSync("/proc/self/stat", "utf8");
const close = stat.lastIndexOf(") ");
const start = stat.slice(close + 2).trim().split(/\s+/)[19];
const api = {
  ui: { router: { current: () => ({type: "session", sessionID: sid}) } },
  data: {
    session: {
      get: (id) => ({id, parentID: null}), family: () => [], status: () => "idle",
      permission: {list: () => []}, form: {list: () => []},
    },
    listen: () => () => {},
  },
};
async function capture(index, identitySid, argv, identityStart = start, entry = tuiV1, startSource = "clear") {
  const state = `${base}/state-${index}`;
  const identityDir = `${state}/hearting/tui-identity`;
  fs.mkdirSync(identityDir, {recursive: true});
  fs.writeFileSync(`${identityDir}/${process.pid}-${start}.json`, JSON.stringify({
    schema: "hearting-tui-selection-v1", sessionID: identitySid, pid: process.pid,
    start: identityStart, sessionSeq: 1700000000123000, sessionStartSource: startSource,
  }));
  const socketPath = `${base}/herdr-${index}.sock`;
  process.env.HERDR_ENV = "1";
  process.env.HERDR_SOCKET_PATH = socketPath;
  process.env.HERDR_PANE_ID = "pane-fixture";
  process.env.XDG_STATE_HOME = state;
  process.argv = argv;
  let settle;
  const received = new Promise((resolve) => { settle = resolve; });
  let reportCount = 0;
  const server = net.createServer((client) => {
    let buffer = "";
    client.on("data", (chunk) => {
      buffer += chunk;
      const newline = buffer.indexOf("\n");
      if (newline < 0) return;
      const request = JSON.parse(buffer.slice(0, newline));
      if (request.method === "pane.report_agent_session") reportCount += 1;
      client.write("{\"result\":{}}\n");
      if (request.method === "pane.report_agent_session") settle(request);
    });
  });
  await new Promise((resolve) => server.listen(socketPath, resolve));
  const dispose = entry.default.setup(api);
  let timeout;
  const request = await Promise.race([received, new Promise((_, reject) =>
    { timeout = setTimeout(() => reject(new Error("session report timeout")), 2500); })]);
  clearTimeout(timeout);
  await new Promise((resolve) => setTimeout(resolve, 25));
  dispose();
  await new Promise((resolve) => server.close(resolve));
  return {params: request.params, processArgv: process.argv.slice(), reportCount};
}
const rawArgv = ["/home/user/.local/bin/opencode", "--session", "A", "--model", "fixture"];
const valid = await capture(1, sid, rawArgv);
const validV2 = await capture(22, sid, rawArgv, start, tuiV2);
if (valid.reportCount !== 1 || validV2.reportCount !== 1) throw Error("copied V1/V2 callback count");
if (validV2.params.agent_session_id !== sid || validV2.params.session_start_source !== "clear") {
  throw Error("copied V2 TUI callback missing");
}
const mismatched = await capture(2, "ses-other", rawArgv);
const changedBirth = await capture(3, sid, rawArgv, "0");
const invalidArgvs = [
  ["bin/opencode", "--session", sid],
  ["/usr/bin/env", "opencode", "--session", sid],
  ["/home/user/.local/bin/opencode", "--session"],
  ["/home/user/.local/bin/opencode", "--session", sid, "\0bad"],
  ["/home/user/.local/bin/opencode", "--session", sid, "x".repeat(8192)],
  ["/home/user/.local/bin/opencode", ...Array(63).fill("x"), "--session", sid],
];
const invalidResults = [];
for (let index = 0; index < invalidArgvs.length; index += 1) {
  invalidResults.push(await capture(index + 4, sid, invalidArgvs[index]));
}
if (valid.params.seq !== 1700000000123000 || valid.params.seq > Number.MAX_SAFE_INTEGER) throw Error("OpenCode seq compatibility");
if (valid.params.session_start_source !== "clear") throw Error("native OpenCode source missing");
if (JSON.stringify(valid.params.resume_argv) !== JSON.stringify(["opencode", "--session", sid, "--model", "fixture"])) throw Error("native OpenCode wire argv missing");
if (JSON.stringify(valid.processArgv) !== JSON.stringify(rawArgv)) throw Error("TUI mutated its raw argv");
if (mismatched.params.session_start_source !== "select" || "seq" in mismatched.params || "resume_argv" in mismatched.params
    || "resume_argv" in changedBirth.params) {
  throw Error("unproven OpenCode identity fields were emitted");
}
for (const [index, item] of invalidResults.entries()) {
  if ("resume_argv" in item.params) throw Error(`invalid TUI argv accepted at ${index}`);
  if (item.params.agent_session_id !== sid || item.params.session_start_source !== "clear"
      || item.params.seq !== 1700000000123000) throw Error(`optional TUI argv erased session identity at ${index}`);
}
for (const [index, item] of cases.entries()) {
  const result = await capture(40 + index, sid, item.argv, start, tuiV1, "startup");
  if (JSON.stringify(result.params.resume_argv ?? null) !== JSON.stringify(item.expected)) {
    throw Error(`shared OpenCode TUI case mismatch: ${item.name}`);
  }
  if (result.params.session_start_source !== "startup") throw Error(`TUI source gate remained at ${item.name}`);
}
console.log(JSON.stringify({valid: valid.params, mismatched: mismatched.params,
  changedBirth: changedBirth.params, invalid: invalidResults.map(item => item.params)}));
'''
        with tempfile.TemporaryDirectory(prefix="Herdr OpenCode session ") as temporary:
            asset_root = Path(temporary)
            (asset_root / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
            shutil.copyfile(ROOT / "adapters/opencode/tui/herdr-tui-session.js",
                            asset_root / "herdr-tui-session.js")
            v2_entry = asset_root / "herdr-opencode/tui.js"
            v2_entry.parent.mkdir(parents=True)
            shutil.copyfile(ROOT / "adapters/opencode/tui/herdr-opencode/tui.js", v2_entry)
            result = subprocess.run(
                ["node", "--input-type=module", "-e", script], cwd=ROOT,
                env={**fixture_env.build_environment(asset_root, ROOT),
                     "HEARTING_TEST_ASSET_ROOT": str(asset_root),
                     "HEARTING_TEST_ROOT": str(ROOT),
                     "HEARTING_TEST_CASES": str(ROOT / "tools/fleet/tests/native_resume_cases.json")},
                text=True, capture_output=True, timeout=15, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn('"session_start_source":"clear"', result.stdout)


class HistoryKeyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config")}
        self.path = herdr._history_path(self.env)
        self.calls = []
        self.which_patch = mock.patch.object(herdr.shutil, "which", return_value="/test/bin/herdr")
        self.which_patch.start()
        self.addCleanup(self.which_patch.stop)

    def runner(self, argv, **kwargs):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def test_exact_restore_and_managed_key(self):
        self.path.parent.mkdir(parents=True)
        original = b"# user config\n[keys]\nprefix = \"ctrl+b\""
        self.path.write_bytes(original)
        result = herdr.ensure_history_key("/tmp/hearting", env=self.env, runner=self.runner)
        self.assertEqual(result["status"], "reloaded")
        self.assertTrue(herdr.history_key_available(self.env))
        self.assertEqual(herdr.history_key_label(self.env), "Ctrl-b Shift-H")
        self.assertEqual(self.calls, [["/test/bin/herdr", "config", "check"],
                                      ["/test/bin/herdr", "server", "reload-config"]])
        self.assertTrue(self.path.read_bytes().endswith(b"\n"))
        restored = herdr.restore_history_key(env=self.env, runner=self.runner)
        self.assertEqual(restored["status"], "restored")
        self.assertEqual(self.path.read_bytes(), original)

    def test_history_key_unavailable_with_an_additional_command_row(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('[keys]\nprefix = "ctrl+b"\n')
        herdr.ensure_history_key("/tmp/hearting", env=self.env, runner=self.runner)
        with self.path.open("a") as config:
            config.write('[[keys.command]]\nkey = "prefix+shift+h"\ntype = "exec"\ncommand = "user-action"\n')
        self.assertFalse(herdr.history_key_available(self.env))

    def test_history_key_unavailable_with_a_user_action_binding(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('[keys]\nprefix = "ctrl+b"\n')
        herdr.ensure_history_key("/tmp/hearting", env=self.env, runner=self.runner)
        contents = self.path.read_text()
        self.path.write_text(contents.replace(
            'prefix = "ctrl+b"\n',
            'prefix = "ctrl+b"\nuser_action = "prefix+shift+h"\n',
            1,
        ))
        self.assertFalse(herdr.history_key_available(self.env))

    def test_key_label_follows_a_custom_prefix(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('[keys]\nprefix = "ctrl+a"\n')
        result = herdr.ensure_history_key("/tmp/hearting", env=self.env, runner=self.runner)
        self.assertEqual(result["status"], "reloaded")
        self.assertEqual(herdr.history_key_label(self.env), "Ctrl-a Shift-H")

    def test_function_key_prefix_is_rendered_readably(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('[keys]\nprefix = "f12"\n')
        herdr.ensure_history_key("/tmp/hearting", env=self.env, runner=self.runner)
        self.assertEqual(herdr.history_key_label(self.env), "F12 Shift-H")

    def test_herdr_config_path_takes_precedence_for_install_and_lookup(self):
        override = self.home / "custom-herdr.toml"
        override.write_text('[keys]\nprefix = "ctrl+a"\n')
        env = {**self.env, "HERDR_CONFIG_PATH": str(override)}
        self.assertEqual(herdr._history_path(env), override)
        result = herdr.ensure_history_key("/tmp/hearting", env=env, runner=self.runner)
        self.assertEqual(result["status"], "reloaded")
        self.assertTrue(herdr.history_key_available(env))
        self.assertEqual(herdr.history_key_label(env), "Ctrl-a Shift-H")
        self.assertFalse(self.path.exists())

    def test_herdr_config_path_expands_home(self):
        env = {**self.env, "HERDR_CONFIG_PATH": "~/herdr-test.toml"}
        self.assertEqual(herdr._history_path(env), self.home / "herdr-test.toml")

    def test_originally_absent_file_is_removed_when_restored(self):
        result = herdr.ensure_history_key("/tmp/hearting", env=self.env, runner=self.runner)
        self.assertEqual(result["status"], "reloaded")
        herdr.restore_history_key(env=self.env, runner=self.runner)
        self.assertFalse(self.path.exists())

    def test_conflicting_key_is_preserved(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('[[keys.command]]\nkey = "prefix+shift+h"\n')
        result = herdr.ensure_history_key("/tmp/hearting", env=self.env, runner=self.runner)
        self.assertEqual(result["status"], "skipped-conflict")
        self.assertEqual(len(self.calls), 0)

    def test_non_command_key_binding_conflict_is_preserved(self):
        self.path.parent.mkdir(parents=True)
        original = '[keys]\nuser_action = "prefix+shift+h"\n'
        self.path.write_text(original)
        result = herdr.ensure_history_key("/tmp/hearting", env=self.env, runner=self.runner)
        self.assertEqual(result["status"], "skipped-conflict")
        self.assertEqual(self.path.read_text(), original)
        self.assertEqual(self.calls, [])

    def test_missing_herdr_does_not_create_config_or_directory(self):
        result = herdr.ensure_history_key("/tmp/hearting", env=self.env, runner=self.runner,
                                          which=lambda name, path=None: None)
        self.assertEqual(result["status"], "skipped-herdr-absent")
        self.assertFalse(self.path.parent.exists())
        self.assertEqual(self.calls, [])

    def test_local_herdr_executable_fallback_is_used(self):
        executable = self.home / ".local/bin/herdr"
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\n")
        executable.chmod(0o700)
        result = herdr.ensure_history_key("/tmp/hearting", env=self.env, runner=self.runner,
                                          which=lambda name, path=None: None)
        self.assertEqual(result["status"], "reloaded")
        self.assertEqual(self.calls[0], [str(executable.resolve()), "config", "check"])

    def test_modified_managed_body_is_preserved(self):
        self.path.parent.mkdir(parents=True)
        herdr.ensure_history_key("/tmp/hearting", env=self.env, runner=self.runner)
        self.path.write_bytes(self.path.read_bytes().replace(b"description =", b"# edited\ndescription ="))
        result = herdr.restore_history_key(env=self.env, runner=self.runner)
        self.assertEqual(result["status"], "preserved-modified")

    def test_config_check_failure_rolls_back_the_added_block(self):
        self.path.parent.mkdir(parents=True)
        original = b"[keys]\nprefix = \"ctrl+b\"\n"
        self.path.write_bytes(original)
        failing = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1 if argv[-1] == "check" else 0)
        result = herdr.ensure_history_key("/tmp/hearting", env=self.env, runner=failing)
        self.assertEqual(result["status"], "rolled-back-check-failed")
        self.assertEqual(self.path.read_bytes(), original)

    def test_config_check_failure_during_restore_reinstates_managed_block(self):
        added = {"done": False}
        def runner(argv, **kwargs):
            if argv[-1] == "check" and added["done"]:
                return subprocess.CompletedProcess(argv, 1)
            return subprocess.CompletedProcess(argv, 0)
        self.path.parent.mkdir(parents=True)
        self.path.write_text("[keys]\nprefix = \"ctrl+b\"\n")
        herdr.ensure_history_key("/tmp/hearting", env=self.env, runner=runner)
        managed = self.path.read_bytes()
        added["done"] = True
        result = herdr.restore_history_key(env=self.env, runner=runner)
        self.assertEqual(result["status"], "rolled-back-restore-check-failed")
        self.assertEqual(self.path.read_bytes(), managed)


if __name__ == "__main__":
    unittest.main()
