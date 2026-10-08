"""Launch isolation must preserve user guards/login and the assigned input."""
import json
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from worker_runtime_home import prepare_worker_home, codex_worker_arguments, claude_worker_arguments
from worker_bootstrap import render_worker_bootstrap

ROOT = Path(__file__).resolve().parents[1]


class WorkerHomes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.source = self.base / 'user'
        self.source.mkdir()
        self.env = {'AGENT_DISPATCH_JOBS': str(self.base / 'dispatch/jobs.log')}

    def tearDown(self):
        self.tmp.cleanup()

    def test_codex_every_type_isolates_global_input_keeps_auth_config_and_hooks(self):
        (self.source / 'agent-config').mkdir()
        (self.source / 'agent-config/models.conf').write_text('user-model-policy')
        for name, content in [('auth.json', '{}'), ('config.toml', '[features]\nhooks=true\n[mcp_servers.external]\ncommand="external"\n'), ('hooks.json', '{"user_guard":true}')]:
            (self.source / name).write_text(content)
        (self.source / 'skills/.system/test-skill').mkdir(parents=True)
        (self.source / 'skills/.system/test-skill/SKILL.md').write_text('test')
        self.env['CODEX_HOME'] = str(self.source)
        before = {p.name: p.read_bytes() for p in self.source.iterdir() if p.is_file()}
        for typ in ('owner', 'stage', 'review', 'support', 'frame'):
            env = prepare_worker_home(ROOT, 'codex', typ, typ, env=self.env)
            home = Path(env['CODEX_HOME'])
            self.assertNotIn('AGENTS.md — Codex Adapter Bootstrap', (home / 'AGENTS.md').read_text())
            for name in before:
                self.assertEqual((home / name).resolve(), (self.source / name).resolve())
            self.assertFalse((home / 'agents').exists())
            self.assertFalse((home / 'plugins').exists())
            self.assertEqual((home / 'agent-config/models.conf').read_text(), 'user-model-policy')
            args = codex_worker_arguments(env)
            self.assertIn('features.multi_agent=false', args)
            self.assertIn('mcp_servers={"external"={"command"="true","enabled"=false}}', args)
            self.assertTrue(any('test-skill' in arg for arg in args))
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.source.iterdir() if p.is_file()})

    def test_claude_guards_permissions_and_credentials_survive_catalog_removal(self):
        (self.source / 'hooks').mkdir()
        (self.source / 'hooks/user-guard.py').write_text('guard')
        settings = {'hooks': {'PreToolUse': [{'hooks': [{'command': 'user-guard'}]}]},
                    'permissions': {'deny': ['Read(secret)']}, 'env': {'USER_GUARD': '1'},
                    'enabledPlugins': {'plugin@market': True}, 'autoMemoryEnabled': True}
        (self.source / 'settings.json').write_text(json.dumps(settings))
        (self.source / '.credentials.json').write_text('{}')
        account = {'accountUuid': 'user-account', 'organizationUuid': 'user-org'}
        (self.source / '.claude.json').write_text(json.dumps({'oauthAccount': account, 'mcpServers': {'main': {}}}))
        self.env['CLAUDE_CONFIG_DIR'] = str(self.source)
        env = prepare_worker_home(ROOT, 'claude', 'support', 'tidy', env=self.env)
        home = Path(env['CLAUDE_CONFIG_DIR'])
        actual = json.loads((home / 'settings.json').read_text())
        for key in ('hooks', 'permissions', 'env'):
            self.assertEqual(actual[key], settings[key])
        self.assertEqual(actual['enabledPlugins'], {'plugin@market': False})
        self.assertFalse(actual['autoMemoryEnabled'])
        self.assertEqual((home / '.credentials.json').resolve(), (self.source / '.credentials.json').resolve())
        self.assertEqual((home / 'hooks/user-guard.py').resolve(), (self.source / 'hooks/user-guard.py').resolve())
        self.assertEqual(json.loads((home / '.claude.json').read_text()), {'oauthAccount': account})
        self.assertIn('--strict-mcp-config', claude_worker_arguments(env))
        self.assertNotIn('--bare', claude_worker_arguments(env))

    def test_codex_hook_alias_preserves_user_decisions_and_rereads_them_on_resume(self):
        hooks = self.source / 'hooks.json'
        hooks.write_text('{"hooks":{}}')
        config = self.source / 'config.toml'
        key = str(hooks) + ':pre_tool_use:0:0'
        config.write_text('[hooks.state.' + json.dumps(key) + ']\ntrusted_hash="sha256:user-approved"\nenabled=false\n')
        self.env['CODEX_HOME'] = str(self.source)
        env = prepare_worker_home(ROOT, 'codex', 'owner', 'trusted-guards', env=self.env)
        before = config.read_bytes()
        state = next(s for s in codex_worker_arguments(env) if s.startswith('hooks.state='))
        self.assertIn(str(Path(env['CODEX_HOME']) / 'hooks.json') + ':pre_tool_use:0:0', state)
        self.assertIn('sha256:user-approved', state)
        self.assertIn('"enabled"=false', state)
        self.assertEqual(config.read_bytes(), before)
        # A resumed supervisor constructs its command again: revocation must
        # not be restored from a previously computed launch environment.
        config.write_text('[hooks.state.' + json.dumps(key) + ']\nenabled=false\n')
        state = next(s for s in codex_worker_arguments(env) if s.startswith('hooks.state='))
        self.assertNotIn('trusted_hash', state)
        self.assertIn('"enabled"=false', state)

    def test_opencode_keeps_deny_and_guard_plugins_excludes_main_and_skills(self):
        config = {'permission': {'bash': {'danger *': 'deny'}},
                  'instructions': [str(ROOT / 'adapters/opencode/AGENTS.md')],
                  'skills': {'paths': ['full-catalog']}, 'provider': {'internal': {'name': 'test'}}}
        (self.source / 'opencode.json').write_text(json.dumps(config))
        (self.source / 'plugins').mkdir()
        (self.source / 'plugins/guard.js').write_text('guard')
        self.env['OPENCODE_CONFIG_DIR'] = str(self.source)
        env = prepare_worker_home(ROOT, 'opencode', 'review', 'review', env=self.env)
        home = Path(env['OPENCODE_CONFIG_DIR'])
        actual = json.loads((home / 'opencode.json').read_text())
        self.assertEqual(actual['permission']['bash'], config['permission']['bash'])
        self.assertEqual(actual['provider'], config['provider'])
        self.assertEqual(actual['instructions'], [])
        self.assertEqual(actual['permission']['skill'], {'*': 'deny'})
        self.assertEqual((home / 'plugins').resolve(), (self.source / 'plugins').resolve())
        self.assertEqual(env['OPENCODE_DISABLE_CLAUDE_CODE_PROMPT'], '1')

    def test_profile_specialization_uses_same_guard_preserving_home(self):
        (self.source / 'settings.json').write_text('{"permissions":{"deny":["Read(secret)"]}}')
        self.env['CLAUDE_CONFIG_DIR'] = str(self.source)
        env = prepare_worker_home(ROOT, 'claude', 'stage', 'specialized', env=self.env, profile='code-report')
        home = Path(env['CLAUDE_CONFIG_DIR'])
        self.assertIn('profiles/code-report.yaml', (home / 'CLAUDE.md').read_text())
        self.assertEqual(json.loads((home / 'settings.json').read_text())['permissions']['deny'], ['Read(secret)'])

    def test_codex_without_optional_user_hook_file_still_builds_minimal_home(self):
        minimal = self.base / 'minimal-source'
        (minimal / 'profiles/templates').mkdir(parents=True)
        (minimal / 'profiles/templates/bootstrap-codex.md').write_text('minimal worker attach')
        self.env['CODEX_HOME'] = str(self.source)
        env = prepare_worker_home(minimal, 'codex', 'review', 'no-user-hooks', env=self.env)
        self.assertEqual((Path(env['CODEX_HOME']) / 'AGENTS.md').read_text(), 'minimal worker attach')
        self.assertFalse((Path(env['CODEX_HOME']) / 'hooks.json').exists())

    def test_liveness_uses_exact_worker_store_before_legacy_profile_or_default(self):
        path = ROOT / 'adapters/codex/bin/dispatch-liveness.py'
        spec = importlib.util.spec_from_file_location('worker_home_liveness', path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        actual = self.base / 'actual-worker'
        pipe = 'profile=legacy-profile,runtime_home=' + str(actual)
        default = self.base / 'other-session/sessions'
        self.assertEqual(mod.sessions_dir_for(pipe, 'job', ROOT, default), actual / 'sessions')
        self.assertEqual(mod.sessions_dirs_for(pipe, 'job', ROOT, default, str(ROOT)), [actual / 'sessions'])


if __name__ == '__main__':
    unittest.main()
