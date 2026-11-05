"""D-79 retirement of MEM_DISTILL_ENABLE and the autoMemoryEnabled managed field.

Exercises `_merge_claude_settings`/`_claude_managed_values`/`_claude_settings_health`
in isolation: a prior release's managed env key must be dropped only when the
user never changed it away, and `autoMemoryEnabled` merges like `statusLine`
(apply when absent, keep-and-report when the user set a conflicting value).
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fixture_env  # noqa: E402
import runtime_activation  # noqa: E402

HOOKS = {"SessionStart": [{"hooks": [{"type": "command", "command": "true"}]}]}
STATUS_LINE = {
    "type": "command",
    "command": runtime_activation.CLAUDE_STATUSLINE_COMMAND,
    "refreshInterval": 60,
}


class ClaudeManagedSettingsTests(unittest.TestCase):
    def setUp(self):
        # These cases isolate retirement and the statusLine/autoMemoryEnabled merge; the
        # identity env keys the shipped settings now manage are covered in
        # runtime_activation.test.py (ClaudeManagedEnvTest).
        patcher = mock.patch.object(runtime_activation, "CLAUDE_MANAGED_ENV_KEYS", ())
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fixture(self, tmp, *, auto_memory=False):
        active_root = Path(tmp) / "source"
        source_settings = active_root / "adapters" / "claude" / "settings.json"
        source_settings.parent.mkdir(parents=True)
        source_settings.write_text(
            json.dumps(
                {
                    "autoMemoryEnabled": auto_memory,
                    "hooks": HOOKS,
                    "statusLine": STATUS_LINE,
                }
            ),
            encoding="utf-8",
        )
        claude_home = Path(tmp) / "claude-home"
        claude_home.mkdir()
        return active_root, claude_home

    def _write_user_settings(self, claude_home, data):
        (claude_home / "settings.json").write_text(json.dumps(data), encoding="utf-8")

    def _read_user_settings(self, claude_home):
        return json.loads((claude_home / "settings.json").read_text(encoding="utf-8"))

    def test_retired_env_key_is_dropped_when_user_never_changed_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            active_root, claude_home = self._fixture(tmp)
            self._write_user_settings(
                claude_home,
                {
                    "hooks": {},
                    "statusLine": STATUS_LINE,
                    "env": {"MEM_DISTILL_ENABLE": "1", "USER_FLAG": "keep"},
                },
            )
            previous = {
                "managed_config": {
                    "claude_hooks": {},
                    "claude_values": {
                        "statusLine": STATUS_LINE,
                        "env": {"MEM_DISTILL_ENABLE": "1"},
                    },
                }
            }
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude_home)}):
                result = runtime_activation._merge_claude_settings(active_root, previous)
            self.assertEqual(result["conflicts"], [])
            settings = self._read_user_settings(claude_home)
            self.assertNotIn("MEM_DISTILL_ENABLE", settings["env"])
            self.assertEqual(settings["env"]["USER_FLAG"], "keep")

    def test_retired_env_key_is_kept_and_reported_when_user_changed_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            active_root, claude_home = self._fixture(tmp)
            self._write_user_settings(
                claude_home,
                {
                    "hooks": {},
                    "statusLine": STATUS_LINE,
                    "env": {"MEM_DISTILL_ENABLE": "0", "USER_FLAG": "keep"},
                },
            )
            previous = {
                "managed_config": {
                    "claude_hooks": {},
                    "claude_values": {
                        "statusLine": STATUS_LINE,
                        "env": {"MEM_DISTILL_ENABLE": "1"},
                    },
                }
            }
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude_home)}):
                result = runtime_activation._merge_claude_settings(active_root, previous)
            self.assertIn("env.MEM_DISTILL_ENABLE", result["conflicts"])
            settings = self._read_user_settings(claude_home)
            self.assertEqual(settings["env"]["MEM_DISTILL_ENABLE"], "0")
            self.assertEqual(settings["env"]["USER_FLAG"], "keep")

    def test_no_env_object_is_forced_when_nothing_is_managed_or_retired(self):
        with tempfile.TemporaryDirectory() as tmp:
            active_root, claude_home = self._fixture(tmp)
            self._write_user_settings(
                claude_home, {"hooks": {}, "statusLine": STATUS_LINE}
            )
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude_home)}):
                result = runtime_activation._merge_claude_settings(active_root, None)
            self.assertEqual(result["conflicts"], [])
            settings = self._read_user_settings(claude_home)
            self.assertNotIn("env", settings)

    def test_auto_memory_enabled_applies_default_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            active_root, claude_home = self._fixture(tmp, auto_memory=False)
            self._write_user_settings(
                claude_home, {"hooks": {}, "statusLine": STATUS_LINE}
            )
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude_home)}):
                result = runtime_activation._merge_claude_settings(active_root, None)
            self.assertEqual(result["conflicts"], [])
            settings = self._read_user_settings(claude_home)
            self.assertIs(settings["autoMemoryEnabled"], False)

    def test_auto_memory_enabled_conflict_keeps_user_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            active_root, claude_home = self._fixture(tmp, auto_memory=False)
            self._write_user_settings(
                claude_home,
                {"hooks": {}, "statusLine": STATUS_LINE, "autoMemoryEnabled": True},
            )
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude_home)}):
                result = runtime_activation._merge_claude_settings(active_root, None)
            self.assertIn("autoMemoryEnabled", result["conflicts"])
            settings = self._read_user_settings(claude_home)
            self.assertIs(settings["autoMemoryEnabled"], True)

    def test_auto_memory_enabled_reapplies_after_user_reverts_to_previous_managed_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            active_root, claude_home = self._fixture(tmp, auto_memory=False)
            self._write_user_settings(
                claude_home,
                {"hooks": {}, "statusLine": STATUS_LINE, "autoMemoryEnabled": False},
            )
            previous = {
                "managed_config": {
                    "claude_hooks": {},
                    "claude_values": {"statusLine": STATUS_LINE, "autoMemoryEnabled": False},
                }
            }
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude_home)}):
                result = runtime_activation._merge_claude_settings(active_root, previous)
            self.assertEqual(result["conflicts"], [])
            settings = self._read_user_settings(claude_home)
            self.assertIs(settings["autoMemoryEnabled"], False)

    def test_health_reports_missing_and_conflicting_auto_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            active_root, claude_home = self._fixture(tmp, auto_memory=False)
            self._write_user_settings(
                claude_home,
                {"hooks": HOOKS, "statusLine": STATUS_LINE},
            )
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude_home)}):
                missing, conflicts = runtime_activation._claude_settings_health(active_root)
            self.assertTrue(missing)
            self.assertEqual(conflicts, [])

            self._write_user_settings(
                claude_home,
                {"hooks": HOOKS, "statusLine": STATUS_LINE, "autoMemoryEnabled": True},
            )
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude_home)}):
                missing, conflicts = runtime_activation._claude_settings_health(active_root)
            self.assertIn("autoMemoryEnabled", conflicts)

    def test_health_ignores_env_when_nothing_is_managed(self):
        with tempfile.TemporaryDirectory() as tmp:
            active_root, claude_home = self._fixture(tmp, auto_memory=False)
            self._write_user_settings(
                claude_home,
                {
                    "hooks": HOOKS,
                    "statusLine": STATUS_LINE,
                    "autoMemoryEnabled": False,
                    "env": {"USER_FLAG": "keep"},
                },
            )
            statusline = claude_home / "statusline.sh"
            statusline.write_text("#!/bin/sh\n", encoding="utf-8")
            statusline.chmod(0o755)
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude_home)}):
                missing, conflicts = runtime_activation._claude_settings_health(active_root)
            self.assertFalse(missing)
            self.assertEqual(conflicts, [])

    def test_idle_health_requires_the_key_only_when_the_source_declares_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            active_root, claude_home = self._fixture(tmp)
            source_path = active_root / "adapters/claude/settings.json"
            source = json.loads(source_path.read_text())
            settings = dict(source)
            self._write_user_settings(claude_home, settings)
            statusline = claude_home / "statusline.sh"
            statusline.write_text("#!/bin/sh\n")
            statusline.chmod(0o755)
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR":str(claude_home)}):
                self.assertEqual(runtime_activation._claude_settings_health(active_root), (False, []))
                source["idleCompaction"] = False
                source_path.write_text(json.dumps(source))
                self.assertTrue(runtime_activation._claude_settings_health(active_root)[0])
                settings["idleCompaction"] = False
                self._write_user_settings(claude_home, settings)
                self.assertEqual(runtime_activation._claude_settings_health(active_root), (False, []))


class ClaudeHookRootHealthTests(unittest.TestCase):
    def _source(self, root: Path) -> tuple[Path, dict]:
        active_root = root / "source"
        (active_root / "adapters/claude").mkdir(parents=True)
        config_root = "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
        hooks = {
            "SessionStart": [{"matcher": "*", "hooks": [
                {"type": "command", "command": f'sh "{config_root}/hooks/run-hook.sh" exec probe.py'},
                {"type": "command", "command": f'sh -c \'exec python3 "{config_root}/tools/memory/mem.py" inject --hook\''},
            ]}],
            "Stop": [{"matcher": "*", "hooks": [
                {"type": "command", "command": f'bash "{config_root}/hooks/herdr-agent-state.sh" session'},
            ]}],
        }
        source = {
            "hooks": hooks,
            "statusLine": STATUS_LINE,
            "autoMemoryEnabled": False,
            "env": {key: "" for key in runtime_activation.CLAUDE_MANAGED_ENV_KEYS},
        }
        (active_root / "adapters/claude/settings.json").write_text(
            json.dumps(source), encoding="utf-8"
        )
        return active_root, source

    @staticmethod
    def _files(home: Path) -> list[Path]:
        paths = [
            home / "hooks/run-hook.sh",
            home / "tools/memory/mem.py",
            home / "hooks/herdr-agent-state.sh",
            home / "statusline.sh",
        ]
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture\n", encoding="utf-8")
            if path.name == "statusline.sh":
                path.chmod(0o755)
        return paths

    def test_health_checks_custom_config_root_with_spaces_and_missing_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            active_root, source = self._source(root)
            custom_home = root / "custom config home"
            settings_path = custom_home / "settings.json"
            settings_path.parent.mkdir(parents=True)
            (custom_home / "hooks").mkdir()
            (custom_home / "tools/memory").mkdir(parents=True)
            files = self._files(custom_home)
            settings_path.write_text(json.dumps({
                **source,
                "hooks": source["hooks"],
            }), encoding="utf-8")
            default_home = root / "default home"
            with fixture_env.patched_environment(root, HERE.parents[1]), \
                    mock.patch.dict(os.environ, {
                        "HOME": str(default_home),
                        "ZDOTDIR": str(default_home),
                        "CLAUDE_CONFIG_DIR": str(custom_home),
                    }), \
                    mock.patch.object(runtime_activation, "_config_path", return_value=settings_path):
                self.assertEqual(runtime_activation._claude_settings_health(active_root), (False, []))
                for path in files:
                    before = path.read_bytes()
                    # destructive-ok: reason=simulate one missing managed hook target for health detection; boundary=one item returned by _files under custom_home in this TemporaryDirectory
                    path.unlink()
                    self.assertTrue(runtime_activation._claude_settings_health(active_root)[0], path)
                    path.write_bytes(before)

    def test_default_home_fallback_health_accepts_old_command_spelling(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            active_root, source = self._source(root)
            home = root / "default home"
            claude_home = home / ".claude"
            self._files(claude_home)
            old_hooks = json.loads(json.dumps(source["hooks"]))
            for groups in old_hooks.values():
                for group in groups:
                    for hook in group["hooks"]:
                        hook["command"] = hook["command"].replace(
                            "${CLAUDE_CONFIG_DIR:-$HOME/.claude}", "$HOME/.claude"
                        )
            settings_path = claude_home / "settings.json"
            settings_path.write_text(json.dumps({**source, "hooks": old_hooks}), encoding="utf-8")
            with fixture_env.patched_environment(root, HERE.parents[1]), \
                    mock.patch.dict(os.environ, {
                        "HOME": str(home),
                        "ZDOTDIR": str(home),
                        "CLAUDE_CONFIG_DIR": "",
                    }), \
                    mock.patch.object(runtime_activation, "_config_path", return_value=settings_path):
                self.assertEqual(runtime_activation._claude_settings_health(active_root), (False, []))


class RetiredHookMigrationTests(unittest.TestCase):
    def setUp(self):
        # These cases isolate retirement and the statusLine/autoMemoryEnabled merge; the
        # identity env keys the shipped settings now manage are covered in
        # runtime_activation.test.py (ClaudeManagedEnvTest).
        patcher = mock.patch.object(runtime_activation, "CLAUDE_MANAGED_ENV_KEYS", ())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_upgrade_retires_only_owned_startup_probes_in_claude_and_codex(self):
        from drivers import claude, codex
        for runtime, driver, filename in (("claude", claude, "settings.json"), ("codex", codex, "hooks.json")):
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                commands = [
                    'sh "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/hooks/run-hook.sh" local-evidence-inject',
                    'python3 "$AGENT_HOME/hooks/dispatch-session-sweep.py"',
                    'bash "$HOME/.claude/hooks/local-evidence-inject.sh"',
                    'sh "$root/adapters/codex/hooks/run-hook.sh" dispatch-session-sweep',
                ]
                owned = [{"type": "command", "command": c} for c in commands]
                user = {"type": "command", "command": "bash /user/hooks/local-evidence-inject.sh"}
                source = {"custom": "keep", "hooks": {
                    "SessionStart": [{"matcher": "*", "hooks": owned + [user]}],
                    "UserPromptSubmit": [{"hooks": owned + [user]}],
                }}
                path = home / filename
                path.write_text(json.dumps(source))
                with mock.patch.object(driver.paths, "runtime_home", return_value=home), \
                     mock.patch.object(driver.projector, "plan", return_value={runtime: []}):
                    before = path.read_bytes()
                    driver.install(dry_run=True)
                    self.assertEqual(path.read_bytes(), before)
                    self.assertFalse(driver.install()["blocked"])
                    result = json.loads(path.read_text())
                    self.assertEqual(result["hooks"]["SessionStart"][0]["hooks"], [user])
                    self.assertEqual(result["hooks"]["UserPromptSubmit"], source["hooks"]["UserPromptSubmit"])
                    self.assertEqual(result["custom"], "keep")
                    once = path.read_bytes()
                    driver.install()
                    self.assertEqual(path.read_bytes(), once)

    def test_copy_once_upgrade_without_activation_manifest_retires_startup_only(self):
        helper = ClaudeManagedSettingsTests()
        with tempfile.TemporaryDirectory() as tmp:
            source, home = helper._fixture(tmp)
            command = {"type": "command", "command":
                       'sh "$HOME/.claude/hooks/run-hook.sh" python3 dispatch-session-sweep.py'}
            helper._write_user_settings(home, {"theme": "keep", "hooks": {
                "SessionStart": [{"hooks": [command]}],
                "UserPromptSubmit": [{"hooks": [command]}]}})
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home)}):
                runtime_activation._merge_claude_settings(source, None)
            result = helper._read_user_settings(home)
            self.assertNotIn(command, [h for g in result["hooks"]["SessionStart"] for h in g["hooks"]])
            self.assertEqual(result["hooks"]["UserPromptSubmit"], [{"hooks": [command]}])
            self.assertEqual(result["theme"], "keep")

    def test_upgrade_prunes_every_retired_hook_and_preserves_user_settings(self):
        import claude_settings_config as settings
        from drivers import claude, codex
        for runtime, driver, filename in (("claude", claude, "settings.json"), ("codex", codex, "hooks.json")):
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                if runtime == "claude":
                    commands = [f'sh "$HOME/.claude/hooks/{name}"' for name in settings._RETIRED_HOOKS]
                    commands.append('"$HOME/.claude/hooks/worker-state-compact.py" guard-write')
                else:
                    commands = ['sh "$root/adapters/codex/hooks/run-hook.sh" pretooluse-write-guard.py']
                user = {"type": "command", "command": "sh /user/hooks/git-state-guard.sh"}
                observer = {"type": "command", "command": 'sh "$HOME/.claude/hooks/spec-read-marker.sh"'}
                data = {"permissions": {"defaultMode": "user-choice"}, "custom": [1, 2], "hooks": {
                    "PreToolUse": [{"matcher": "*", "timeout": 99, "hooks": [
                        *({"type": "command", "command": c} for c in commands), user]}],
                    "PostToolUse": [{"hooks": [observer]}]}}
                path = home / filename
                path.write_text(json.dumps(data))
                entries = [] if runtime == "claude" else [{"action": "symlink", "source": str(home / "source-hooks.json"), "dest": str(path)}]
                with mock.patch.object(driver.paths, "runtime_home", return_value=home), mock.patch.object(driver.projector, "plan", return_value={runtime: entries}):
                    before = path.read_bytes()
                    driver.install(dry_run=True)
                    self.assertEqual(path.read_bytes(), before)
                    result = driver.install()
                    self.assertFalse(result["blocked"], result)
                    updated = json.loads(path.read_text())
                    self.assertEqual(updated["hooks"]["PreToolUse"][0]["hooks"], [user])
                    self.assertEqual(updated["hooks"]["PostToolUse"], data["hooks"]["PostToolUse"])
                    self.assertEqual(updated["permissions"], data["permissions"])
                    self.assertEqual(updated["custom"], data["custom"])
                    once = path.read_bytes()
                    driver.install()
                    self.assertEqual(path.read_bytes(), once)

    def test_activation_without_old_manifest_removes_retired_hook(self):
        helper = ClaudeManagedSettingsTests()
        with tempfile.TemporaryDirectory() as tmp:
            source, home = helper._fixture(tmp)
            user_hook = {"type": "command", "command": "echo user"}
            helper._write_user_settings(home, {"hooks": {"PreToolUse": [{"hooks": [
                {"type": "command", "command": 'sh "$HOME/.claude/hooks/artifact-guard.sh"'}, user_hook]}]}})
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home)}):
                runtime_activation._merge_claude_settings(source, None)
            self.assertEqual(helper._read_user_settings(home)["hooks"]["PreToolUse"][0]["hooks"], [user_hook])

    def test_retired_commands_cover_both_config_root_spellings_and_preserve_foreign(self):
        import claude_settings_config as settings
        old_root = "$HOME/.claude/hooks/"
        new_root = "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/hooks/"
        user_hook = {"type": "command", "command": "/home/user/hooks/core-first-guard.sh"}
        live_hook = {"type": "command", "command": f'sh "{new_root}spec-read-marker.sh"'}
        data = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": f'sh "{old_root}core-first-guard.sh"'},
            {"type": "command", "command": f'sh "{new_root}core-first-guard.sh"'},
            user_hook,
            live_hook,
        ]}]}}
        self.assertTrue(settings.remove_retired_hooks(data))
        self.assertEqual(data["hooks"]["PreToolUse"][0]["hooks"], [user_hook, live_hook])

    def test_retired_guard_called_through_the_harness_root_is_pruned(self):
        # Settings that reached a guard through the runtime-home `hearting`
        # projection, the managed `current` pointer, or a release directory
        # still name an owned file after the guard is retired; a missing script
        # there exits 2, which Claude reads as a block on every Edit/Write.
        import claude_settings_config as settings
        owned = [
            'sh "$HOME/.claude/hearting/hooks/core-first-guard.sh"',
            'sh "$HOME/.claude/hearting/hooks/core-read-marker.sh"',
            'sh "$HOME/hearting/hooks/git-state-guard.sh"',
            'sh "$HOME/.local/share/hearting/current/hooks/artifact-guard.sh"',
            'sh "/home/u/.local/share/hearting/releases/v2.150.0/hooks/spec-skill-gate.sh"',
        ]
        user = [
            {"type": "command", "command": 'sh "$HOME/scripts/core-first-guard.sh"'},
            {"type": "command", "command": 'sh "$HOME/.claude/hearting/hooks/herdr-agent-state.sh" idle'},
        ]
        data = {"hooks": {"PreToolUse": [{"matcher": "Edit|Write|MultiEdit", "hooks": [
            *({"type": "command", "command": c} for c in owned), *user]}]}}
        self.assertTrue(settings.remove_retired_hooks(data))
        self.assertEqual(data["hooks"]["PreToolUse"][0]["hooks"], user)

OLD_MEM_SYNC = (
    "sh -c 'if [ \"${AGENT_SESSION_ROLE:-}\" = worker ] || [ \"${AGENT_DISPATCH_CHILD:-}\" = 1 ] "
    "|| [ -n \"${AGENT_DISPATCH_DEPTH:-}\" ] || [ -n \"${OPENCODE_DISPATCH_SLUG:-}\" ] "
    "|| [ \"${FLEET_TITLE_REFRESH:-}\" = 1 ] || [ \"${MEM_DISTILL:-}\" = 1 ]; then exit 0; fi; "
    "exec python3 \"$HOME/.claude/tools/memory/mem.py\" sync --json >/dev/null'"
)
FLEET_END = 'sh "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/hooks/run-hook.sh" python3 fleet-interaction-state.py clear'
HERDR_END = 'sh "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/hooks/run-hook.sh" bash herdr-agent-state.sh release'


class SessionEndMemorySyncRetirementTests(unittest.TestCase):
    """D-82: the managed SessionEnd `mem.py sync` leaves installed settings, nothing else does."""
    def setUp(self):
        # These cases isolate retirement and the statusLine/autoMemoryEnabled merge; the
        # identity env keys the shipped settings now manage are covered in
        # runtime_activation.test.py (ClaudeManagedEnvTest).
        patcher = mock.patch.object(runtime_activation, "CLAUDE_MANAGED_ENV_KEYS", ())
        patcher.start()
        self.addCleanup(patcher.stop)


    @staticmethod
    def _group(command, timeout=10):
        return {"matcher": "*", "hooks": [{"type": "command", "command": command, "timeout": timeout}]}

    def _installed(self, extra=()):
        return {"hooks": {"SessionEnd": [
            self._group(FLEET_END), self._group(HERDR_END), self._group(OLD_MEM_SYNC, 120), *extra]}}

    def _commands(self, data):
        return [h["command"] for g in data["hooks"]["SessionEnd"] for h in g["hooks"]]

    def _source(self, tmp):
        helper = ClaudeManagedSettingsTests()
        active_root, claude_home = helper._fixture(tmp)
        settings_path = active_root / "adapters" / "claude" / "settings.json"
        source = json.loads(settings_path.read_text(encoding="utf-8"))
        source["hooks"] = {"SessionEnd": [self._group(FLEET_END), self._group(HERDR_END)]}
        settings_path.write_text(json.dumps(source), encoding="utf-8")
        return helper, active_root, claude_home

    def test_install_with_an_old_manifest_retires_only_the_managed_sync(self):
        user_end = {"matcher": "*", "hooks": [{"type": "command", "command": "echo user-end"}]}
        user_sync = self._group('python3 "$HOME/bin/my-own-mem.py" sync')
        with tempfile.TemporaryDirectory() as tmp:
            helper, source, home = self._source(tmp)
            helper._write_user_settings(home, self._installed([user_end, user_sync]))
            previous = {"managed_config": {"claude_hooks": {"SessionEnd": [
                self._group(FLEET_END), self._group(HERDR_END), self._group(OLD_MEM_SYNC, 120)]}}}
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home)}):
                runtime_activation._merge_claude_settings(source, previous)
            self.assertCountEqual(
                self._commands(helper._read_user_settings(home)),
                [FLEET_END, "echo user-end", 'python3 "$HOME/bin/my-own-mem.py" sync'],
            )

    def test_install_without_a_manifest_retires_only_the_managed_sync(self):
        user_end = {"matcher": "*", "hooks": [{"type": "command", "command": "echo user-end"}]}
        # A user's own hook that merely looks alike: other path, no worker guard, other verb.
        lookalikes = [
            self._group('python3 "$HOME/.claude/tools/memory/mem.py" sync --json'),
            self._group("sh -c 'exec python3 \"$HOME/.claude/tools/memory/mem.py\" sync --json'"),
            self._group(OLD_MEM_SYNC.replace("sync --json", "inject --hook")),
            self._group(OLD_MEM_SYNC.replace("$HOME/.claude/tools", "$HOME/mine")),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            helper, source, home = self._source(tmp)
            helper._write_user_settings(home, self._installed([user_end, *lookalikes]))
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home)}):
                runtime_activation._merge_claude_settings(source, None)
            got = self._commands(helper._read_user_settings(home))
            self.assertNotIn(OLD_MEM_SYNC, got)
            self.assertEqual(got.count(FLEET_END), 1)
            self.assertEqual(got.count(HERDR_END), 0)
            self.assertEqual(len(got), 1 + 1 + len(lookalikes), got)
            self.assertIn("echo user-end", got)
            for group in lookalikes:
                self.assertIn(group["hooks"][0]["command"], got)

    def test_older_spellings_of_the_managed_sync_are_retired_too(self):
        older = OLD_MEM_SYNC.replace(" >/dev/null", "").replace("exec python3", "exec env MEM_DUMP_PUSH=1 python3")
        data = {"hooks": {"SessionEnd": [self._group(older)]}}
        import claude_settings_config as settings
        self.assertTrue(settings.remove_retired_hooks(data))
        self.assertNotIn("SessionEnd", data["hooks"])

    def test_the_sync_command_is_only_retired_under_session_end(self):
        data = {"hooks": {"Stop": [self._group(OLD_MEM_SYNC)]}}
        import claude_settings_config as settings
        self.assertFalse(settings.remove_retired_hooks(data))
        self.assertEqual(self._commands({"hooks": {"SessionEnd": data["hooks"]["Stop"]}}), [OLD_MEM_SYNC])

    def test_the_shipped_template_moves_herdr_to_session_start_and_keeps_fleet(self):
        template = json.loads((HERE.parents[1] / "adapters/claude/settings.json").read_text(encoding="utf-8"))
        commands = self._commands(template)
        self.assertEqual(commands, [FLEET_END])
        self.assertFalse([c for c in commands if "mem.py" in c])
        start = [hook["command"] for group in template["hooks"]["SessionStart"]
                 for hook in group.get("hooks", [])]
        self.assertEqual(start.count(runtime_activation.herdr_integration.CLAUDE_SESSION_COMMAND), 1)


if __name__ == "__main__":
    unittest.main()
