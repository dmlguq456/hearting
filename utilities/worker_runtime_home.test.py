"""Launch isolation must preserve user guards/login and the assigned input."""
import json
import importlib.util
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import worker_runtime_home
from worker_runtime_home import prepare_worker_home, codex_worker_arguments, claude_worker_arguments
from worker_bootstrap import render_worker_bootstrap
from claude_credential_recovery import SECURE_STORAGE_ENV
import dispatch_capacity_evidence

ROOT = Path(__file__).resolve().parents[1]


class WorkerHomes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        # The launch observation needs an `opencode` on PATH even when the
        # version is pinned; keep the suite independent of the host install.
        fake_bin = self.base / 'fake-bin'
        fake_bin.mkdir()
        fake_opencode = fake_bin / 'opencode'
        fake_opencode.write_text('#!/bin/sh\necho 1.19.0\n', encoding='utf-8')
        fake_opencode.chmod(0o755)
        runtime_version = patch.dict(os.environ, {
            'OPENCODE_RUNTIME_VERSION': '1.19.0',
            'PATH': os.pathsep.join(filter(None, (str(fake_bin), os.environ.get('PATH')))),
        })
        runtime_version.start()
        self.addCleanup(runtime_version.stop)
        self.source = self.base / 'user'
        self.source.mkdir()
        self.env = {'AGENT_DISPATCH_JOBS': str(self.base / 'dispatch/jobs.log')}
        self._module_snapshot = {
            name: sys.modules.get(name)
            for name in ('dispatch_allocation', 'claude_credential_recovery')
        }

    def tearDown(self):
        self.tmp.cleanup()
        for name, module in self._module_snapshot.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def _account_defaults(self, accounts):
        path = self.base / 'dispatch-defaults.yaml'
        shipped = (ROOT / 'profiles/dispatch-defaults.yaml').read_text(encoding='utf-8')
        lines = [shipped.rstrip(), '', 'worker_account:']
        lines.extend(f'  {harness}: {value}' for harness, value in accounts.items())
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return path

    def _minimal_packaged_builder(self):
        """Load the real builder and resolver from a package root without profiles/."""
        root = self.base / 'packaged-root'
        files = (
            'tools/profile/build-home.py',
            'utilities/dispatch-defaults.py',
            'utilities/dispatch_allocation.py',
            'utilities/claude_credential_recovery.py',
            'utilities/opencode_runtime.py',
            'capabilities/topologies.json',
            'profiles/templates/bootstrap-codex.md',
            'profiles/templates/bootstrap-claude.md',
            'profiles/templates/bootstrap-opencode.md',
            'adapters/codex/hooks/hooks.json',
            'adapters/codex/config/models.conf',
        )
        for relative in files:
            source = ROOT / relative
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        self.assertFalse((root / 'profiles/dispatch-defaults.yaml').exists())
        # The copied resolver imports this sibling by its top-level module
        # name. Load the fixture copy so it cannot accidentally resolve the
        # implementation from the real checkout's utilities directory.
        allocation_path = root / 'utilities/dispatch_allocation.py'
        allocation_spec = importlib.util.spec_from_file_location(
            'dispatch_allocation', allocation_path)
        allocation = importlib.util.module_from_spec(allocation_spec)
        sys.modules[allocation_spec.name] = allocation
        allocation_spec.loader.exec_module(allocation)
        recovery_path = root / 'utilities/claude_credential_recovery.py'
        recovery_spec = importlib.util.spec_from_file_location(
            'claude_credential_recovery', recovery_path)
        recovery = importlib.util.module_from_spec(recovery_spec)
        sys.modules[recovery_spec.name] = recovery
        recovery_spec.loader.exec_module(recovery)
        path = root / 'tools/profile/build-home.py'
        spec = importlib.util.spec_from_file_location(
            f'worker_home_builder_{id(self)}', path)
        builder = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = builder
        spec.loader.exec_module(builder)
        return root, builder

    def test_minimal_packaged_root_honors_user_defaults_for_all_harnesses(self):
        root, builder = self._minimal_packaged_builder()
        home = self.base / 'minimal-user-home'
        xdg = self.base / 'minimal-user-xdg'
        codex_parent = self.base / 'minimal-parent-codex'
        codex_account = self.base / 'minimal-account-codex'
        claude_parent = self.base / 'minimal-parent-claude'
        claude_account = self.base / 'minimal-account-claude'
        opencode_parent = self.base / 'minimal-parent-opencode'
        opencode_account = self.base / 'minimal-account-opencode'
        xdg_data = self.base / 'minimal-parent-xdg-data'
        for path in (home, xdg, codex_parent, codex_account, claude_parent,
                     claude_account, opencode_parent, opencode_account):
            path.mkdir(parents=True)
        (codex_parent / 'auth.json').write_text('{"account":"parent"}')
        (codex_account / 'auth.json').write_text('{"account":"selected"}')
        for directory, account in ((claude_parent, 'parent'), (claude_account, 'selected')):
            (directory / '.claude.json').write_text(json.dumps({
                'oauthAccount': {'accountUuid': account, 'organizationUuid': account},
            }))
        (opencode_parent / 'opencode.json').write_text('{}')
        (opencode_account / 'opencode').mkdir()
        (opencode_account / 'opencode/auth.json').write_text('{"account":"selected"}')
        (xdg_data / 'opencode').mkdir(parents=True)
        (xdg_data / 'opencode/auth.json').write_text('{"account":"parent"}')
        defaults_dir = xdg / 'hearting'
        defaults_dir.mkdir()
        defaults = defaults_dir / 'dispatch-defaults.yaml'
        defaults.write_text(self._account_defaults({
            'codex': str(codex_account), 'claude': str(claude_account),
            'opencode': str(opencode_account),
        }).read_text(encoding='utf-8'), encoding='utf-8')
        env = {**self.env, 'HOME': str(home), 'XDG_CONFIG_HOME': str(xdg),
               'CODEX_HOME': str(codex_parent), 'CLAUDE_CONFIG_DIR': str(claude_parent),
               'OPENCODE_CONFIG_DIR': str(opencode_parent), 'XDG_DATA_HOME': str(xdg_data)}
        self.assertNotIn('DISPATCH_DEFAULTS_CONFIG', env)
        self.assertFalse((root / 'profiles/dispatch-defaults.yaml').exists())

        codex = builder.build_worker_home(root, 'codex', 'stage', 'user-file-codex',
                                           env=env, destination=self.base / 'user-codex-home')
        self.assertEqual((Path(codex['CODEX_HOME']) / 'auth.json').resolve(),
                         (codex_account / 'auth.json').resolve())
        claude = builder.build_worker_home(root, 'claude', 'stage', 'user-file-claude',
                                            env=env, destination=self.base / 'user-claude-home')
        self.assertEqual(json.loads((Path(claude['CLAUDE_CONFIG_DIR']) / '.claude.json').read_text())[
            'oauthAccount']['accountUuid'], 'selected')
        opencode = builder.build_worker_home(root, 'opencode', 'stage', 'user-file-opencode',
                                              env=env, destination=self.base / 'user-opencode-home')
        self.assertEqual((Path(opencode['XDG_DATA_HOME']) / 'opencode/auth.json').resolve(),
                         (opencode_account / 'opencode/auth.json').resolve())

        defaults.write_text('schema_version: [\n', encoding='utf-8')
        with self.assertRaises(builder._dispatch_defaults_module().DefaultsConfigError):
            builder._worker_account_selection('codex', env)

    def test_reused_homes_remove_only_stale_explicit_auth_links(self):
        root, builder = self._minimal_packaged_builder()
        parent = self.base / 'reuse-parent-codex'
        parent.mkdir()
        (parent / 'auth.json').write_text('{"account":"parent"}', encoding='utf-8')
        opencode_parent = self.base / 'reuse-parent-xdg-data'
        (opencode_parent / 'opencode').mkdir(parents=True)
        (opencode_parent / 'opencode/auth.json').write_text(
            '{"account":"parent"}', encoding='utf-8')
        selected_codex = self.base / 'reuse-selected-codex'
        selected_opencode = self.base / 'reuse-selected-opencode'
        selected_codex.mkdir()
        selected_opencode.mkdir()
        defaults = self._account_defaults({
            'codex': str(selected_codex), 'opencode': str(selected_opencode),
        })
        codex_dest = self.base / 'reuse-codex-home'
        codex_env = {**self.env, 'HOME': str(self.base / 'reuse-home'),
                     'CODEX_HOME': str(parent)}
        first = builder.build_worker_home(root, 'codex', 'stage', 'reuse-codex',
                                          env=codex_env, destination=codex_dest)
        codex_auth = Path(first['CODEX_HOME']) / 'auth.json'
        self.assertEqual(codex_auth.resolve(), (parent / 'auth.json').resolve())
        builder.build_worker_home(root, 'codex', 'stage', 'reuse-codex',
                                  env={**codex_env, 'DISPATCH_DEFAULTS_CONFIG': str(defaults)},
                                  destination=codex_dest)
        self.assertFalse(codex_auth.exists())

        opencode_config = self.base / 'reuse-opencode-config'
        opencode_config.mkdir()
        (opencode_config / 'opencode.json').write_text('{}', encoding='utf-8')
        opencode_dest = self.base / 'reuse-opencode-home'
        opencode_env = {**self.env, 'HOME': str(self.base / 'reuse-home'),
                        'OPENCODE_CONFIG_DIR': str(opencode_config),
                        'XDG_DATA_HOME': str(opencode_parent)}
        first = builder.build_worker_home(root, 'opencode', 'stage', 'reuse-opencode',
                                          env=opencode_env, destination=opencode_dest)
        opencode_auth = Path(first['XDG_DATA_HOME']) / 'opencode/auth.json'
        self.assertEqual(opencode_auth.resolve(),
                         (opencode_parent / 'opencode/auth.json').resolve())
        builder.build_worker_home(root, 'opencode', 'stage', 'reuse-opencode',
                                  env={**opencode_env, 'DISPATCH_DEFAULTS_CONFIG': str(defaults)},
                                  destination=opencode_dest)
        self.assertFalse(opencode_auth.exists())

        for harness, auth_path, env, dest in (
                ('codex', self.base / 'regular-codex-home/auth.json', codex_env,
                 self.base / 'regular-codex-home'),
                ('opencode', self.base / 'regular-opencode-home/runtime/data/opencode/auth.json',
                 opencode_env, self.base / 'regular-opencode-home')):
            auth_path.parent.mkdir(parents=True, exist_ok=True)
            auth_path.write_text('preserve regular file', encoding='utf-8')
            builder.build_worker_home(
                root, harness, 'stage', 'regular-auth-' + harness,
                env={**env, 'DISPATCH_DEFAULTS_CONFIG': str(defaults)}, destination=dest)
            self.assertEqual(auth_path.read_text(encoding='utf-8'), 'preserve regular file')

    def test_missing_implicit_defaults_keeps_all_harness_auth_and_settings(self):
        root, builder = self._minimal_packaged_builder()
        parent_home = self.base / 'packaged-parent-home'
        parent_home.mkdir()

        codex_source = self.base / 'packaged-parent-codex'
        codex_source.mkdir()
        (codex_source / 'auth.json').write_text('{"account":"parent"}', encoding='utf-8')
        (codex_source / 'config.toml').write_text('[user]\nvalue="parent"\n', encoding='utf-8')
        (codex_source / 'hooks.json').write_text('{"user_guard":true}', encoding='utf-8')
        codex = builder.build_worker_home(
            root, 'codex', 'stage', 'implicit-config-codex',
            env={**self.env, 'HOME': str(parent_home), 'CODEX_HOME': str(codex_source)},
            destination=self.base / 'packaged-codex-worker')
        codex_home = Path(codex['CODEX_HOME'])
        self.assertEqual((codex_home / 'auth.json').resolve(),
                         (codex_source / 'auth.json').resolve())
        self.assertEqual((codex_home / 'config.toml').resolve(),
                         (codex_source / 'config.toml').resolve())
        self.assertEqual((codex_home / 'hooks.json').resolve(),
                         (codex_source / 'hooks.json').resolve())
        self.assertEqual(codex['HEARTING_MODEL_CONFIG_HOME'], str(codex_source))

        claude_source = self.base / 'packaged-parent-claude'
        claude_source.mkdir()
        claude_account = {'accountUuid': 'parent-account', 'organizationUuid': 'parent-org'}
        (claude_source / '.claude.json').write_text(
            json.dumps({'oauthAccount': claude_account}), encoding='utf-8')
        claude_settings = {
            'env': {'PARENT_SETTING': 'kept'},
            'hooks': {'PreToolUse': [{'hooks': [{'command': 'parent-guard'}]}]},
            'permissions': {'deny': ['Read(secret)']},
        }
        (claude_source / 'settings.json').write_text(
            json.dumps(claude_settings), encoding='utf-8')
        claude = builder.build_worker_home(
            root, 'claude', 'stage', 'implicit-config-claude',
            env={**self.env, 'HOME': str(parent_home),
                 'CLAUDE_CONFIG_DIR': str(claude_source)},
            destination=self.base / 'packaged-claude-worker')
        claude_home = Path(claude['CLAUDE_CONFIG_DIR'])
        self.assertEqual(json.loads((claude_home / '.claude.json').read_text()),
                         {'oauthAccount': claude_account})
        projected_settings = json.loads((claude_home / 'settings.json').read_text())
        for key in ('env', 'hooks', 'permissions'):
            self.assertEqual(projected_settings[key], claude_settings[key])
        self.assertEqual(claude['HEARTING_MODEL_CONFIG_HOME'], str(claude_source))

        opencode_source = self.base / 'packaged-parent-opencode'
        opencode_source.mkdir()
        (opencode_source / 'opencode.json').write_text(json.dumps({
            'model': 'provider/parent-model',
            'permission': {'bash': {'danger *': 'deny'}},
        }), encoding='utf-8')
        xdg_data = self.base / 'packaged-parent-xdg-data'
        (xdg_data / 'opencode').mkdir(parents=True)
        (xdg_data / 'opencode/auth.json').write_text(
            '{"account":"parent"}', encoding='utf-8')
        opencode = builder.build_worker_home(
            root, 'opencode', 'stage', 'implicit-config-opencode',
            env={**self.env, 'HOME': str(parent_home),
                 'OPENCODE_CONFIG_DIR': str(opencode_source),
                 'XDG_DATA_HOME': str(xdg_data)},
            destination=self.base / 'packaged-opencode-worker')
        opencode_home = Path(opencode['OPENCODE_CONFIG_DIR'])
        projected_opencode = json.loads((opencode_home / 'opencode.json').read_text())
        self.assertEqual(projected_opencode['model'], 'provider/parent-model')
        self.assertEqual(projected_opencode['permission']['bash'],
                         {'danger *': 'deny'})
        self.assertEqual(projected_opencode['permission']['skill'], {'*': 'deny'})
        self.assertFalse(projected_opencode['snapshot'])
        self.assertEqual((opencode_home / 'runtime/data/opencode/auth.json').resolve(),
                         (xdg_data / 'opencode/auth.json').resolve())
        self.assertEqual(opencode['HEARTING_MODEL_CONFIG_HOME'], str(opencode_source))

    def test_explicit_defaults_override_errors_are_not_ignored(self):
        root, builder = self._minimal_packaged_builder()
        missing = self.base / 'requested-dispatch-defaults.yaml'
        with self.assertRaises(FileNotFoundError):
            builder._worker_account_selection(
                'codex', {**self.env, 'DISPATCH_DEFAULTS_CONFIG': str(missing)})

        malformed = self.base / 'malformed-dispatch-defaults.yaml'
        malformed.write_text('schema_version: [\n', encoding='utf-8')
        defaults = builder._dispatch_defaults_module()
        with self.assertRaises(defaults.DefaultsConfigError):
            builder._worker_account_selection(
                'codex', {**self.env, 'DISPATCH_DEFAULTS_CONFIG': str(malformed)})

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
            self.assertNotIn(SECURE_STORAGE_ENV, env)
            self.assertEqual(env['HEARTING_MODEL_CONFIG_HOME'], str(self.source))
            self.assertEqual(env['HEARTING_MODEL_CONFIG_HOME'], str(self.source))
            home = Path(env['CODEX_HOME'])
            self.assertNotIn('AGENTS.md — Codex Adapter Bootstrap', (home / 'AGENTS.md').read_text())
            for name in before:
                self.assertEqual((home / name).resolve(), (self.source / name).resolve())
            self.assertFalse((home / 'agents').exists())
            self.assertFalse((home / 'plugins').exists())
            self.assertEqual((home / 'agent-config/models.conf').read_text(), 'user-model-policy')
            args = codex_worker_arguments(env)
            self.assertIn('features.plugins=false', args)
            self.assertIn('features.multi_agent=false', args)
            self.assertIn('features.default_mode_request_user_input=false', args)
            self.assertIn('mcp_servers={"external"={"command"="true","enabled"=false}}', args)
            self.assertTrue(any('test-skill' in arg for arg in args))
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.source.iterdir() if p.is_file()})

    def test_nested_codex_owner_uses_plugin_feature_override_and_is_destination(self):
        (self.source / 'auth.json').write_text('{}')
        (self.source / 'config.toml').write_text('[features]\nhooks=true\n')
        self.env['CODEX_HOME'] = str(self.source)
        destination = self.base / 'nested-owner-home'

        values = prepare_worker_home(ROOT, 'codex', 'owner', 'nested-owner',
                                     env=self.env, destination=destination)

        self.assertIn('features.plugins=false', codex_worker_arguments(values))
        self.assertEqual(Path(values['CODEX_HOME']).resolve(), destination.resolve())
        manifest = json.loads((destination / 'worker-home.json').read_text())
        self.assertTrue(manifest['destination'])

    def test_worker_accounts_select_only_auth_sources_and_keep_settings_on_parent(self):
        parent_home = self.base / 'parent-home'
        parent_codex = self.base / 'parent-codex'
        parent_claude = self.base / 'parent-claude'
        parent_opencode_config = self.base / 'parent-opencode-config'
        parent_xdg_data = self.base / 'parent-xdg-data'
        selected_codex = self.base / 'accounts/codex'
        selected_claude = self.base / 'accounts/claude'
        selected_opencode_data = self.base / 'accounts/opencode-data'
        parent_store = self.base / 'parent-secure-store'
        selected_store = self.base / 'selected-secure-store'
        for path in (parent_home, parent_codex, parent_claude,
                     parent_opencode_config, parent_xdg_data,
                     selected_codex, selected_claude, selected_opencode_data,
                     parent_store, selected_store):
            path.mkdir(parents=True)

        (parent_codex / 'auth.json').write_text('{"account":"A"}', encoding='utf-8')
        (parent_codex / 'config.toml').write_text('[user]\nvalue="A"\n', encoding='utf-8')
        (parent_codex / 'hooks.json').write_text('{"user_guard":true}', encoding='utf-8')
        (selected_codex / 'auth.json').write_text('{"account":"B"}', encoding='utf-8')

        parent_claude_hooks = parent_claude / 'hooks'
        parent_claude_hooks.mkdir()
        (parent_claude_hooks / 'guard.py').write_text('parent guard', encoding='utf-8')
        parent_settings = {
            'env': {SECURE_STORAGE_ENV: str(parent_store), 'PARENT_SETTING': 'kept'},
            'hooks': {'PreToolUse': [{'hooks': [{'command': 'parent-guard'}]}]},
            'permissions': {'deny': ['Read(secret)']},
        }
        (parent_claude / 'settings.json').write_text(json.dumps(parent_settings), encoding='utf-8')
        parent_claude_account = {'accountUuid': 'account-A', 'organizationUuid': 'org-A'}
        (parent_claude / '.claude.json').write_text(
            json.dumps({'oauthAccount': parent_claude_account}), encoding='utf-8')
        (parent_claude / '.credentials.json').write_text('{"credential":"A"}', encoding='utf-8')
        selected_claude_account = {'accountUuid': 'account-B', 'organizationUuid': 'org-B'}
        (selected_claude / '.claude.json').write_text(
            json.dumps({'oauthAccount': selected_claude_account}), encoding='utf-8')
        selected_claude_settings = {'env': {SECURE_STORAGE_ENV: str(selected_store)}}
        (selected_claude / 'settings.json').write_text(
            json.dumps(selected_claude_settings), encoding='utf-8')
        (selected_claude / '.credentials.json').write_text('{"credential":"B"}', encoding='utf-8')

        (parent_opencode_config / 'opencode.json').write_text(
            json.dumps({'model': 'provider/model-A', 'theme': 'parent-theme'}), encoding='utf-8')
        (parent_xdg_data / 'opencode').mkdir()
        (parent_xdg_data / 'opencode/auth.json').write_text(
            '{"account":"A"}', encoding='utf-8')
        (selected_opencode_data / 'opencode').mkdir(parents=True)
        (selected_opencode_data / 'opencode/auth.json').write_text(
            '{"account":"B"}', encoding='utf-8')

        defaults_path = self._account_defaults({
            'claude': str(selected_claude),
            'codex': str(selected_codex),
            'opencode': str(selected_opencode_data),
        })
        env = {
            **self.env,
            'HOME': str(parent_home),
            'DISPATCH_DEFAULTS_CONFIG': str(defaults_path),
            'CODEX_HOME': str(parent_codex),
            'CLAUDE_CONFIG_DIR': str(parent_claude),
            'CLAUDE_SECURESTORAGE_CONFIG_DIR': str(parent_store),
            'OPENCODE_CONFIG_DIR': str(parent_opencode_config),
            'XDG_CONFIG_HOME': str(self.base / 'parent-xdg-config'),
            'XDG_DATA_HOME': str(parent_xdg_data),
        }
        original_bytes = {
            path: path.read_bytes()
            for path in (parent_codex / 'auth.json', parent_codex / 'config.toml',
                         parent_claude / 'settings.json', parent_claude / '.claude.json',
                         parent_claude / '.credentials.json',
                         selected_claude / '.credentials.json',
                         parent_opencode_config / 'opencode.json',
                         parent_xdg_data / 'opencode/auth.json',
                         selected_opencode_data / 'opencode/auth.json')
        }

        codex = prepare_worker_home(ROOT, 'codex', 'stage', 'account-b-codex', env=env)
        codex_home = Path(codex['CODEX_HOME'])
        self.assertEqual((codex_home / 'auth.json').resolve(), (selected_codex / 'auth.json').resolve())
        self.assertEqual((codex_home / 'config.toml').resolve(), (parent_codex / 'config.toml').resolve())
        self.assertEqual((codex_home / 'hooks.json').resolve(), (parent_codex / 'hooks.json').resolve())
        self.assertEqual(codex['HEARTING_MODEL_CONFIG_HOME'], str(parent_codex))

        claude = prepare_worker_home(ROOT, 'claude', 'stage', 'account-b-claude', env=env)
        claude_home = Path(claude['CLAUDE_CONFIG_DIR'])
        self.assertEqual(json.loads((claude_home / '.claude.json').read_text()),
                         {'oauthAccount': selected_claude_account})
        self.assertEqual(claude[SECURE_STORAGE_ENV], str(selected_store.resolve()))
        claude_settings = json.loads((claude_home / 'settings.json').read_text())
        self.assertEqual(claude_settings['env'][SECURE_STORAGE_ENV], str(selected_store.resolve()))
        self.assertEqual(claude_settings['env']['PARENT_SETTING'], 'kept')
        self.assertEqual(claude_settings['hooks'], parent_settings['hooks'])
        self.assertEqual(claude_settings['permissions'], parent_settings['permissions'])
        self.assertEqual((claude_home / 'hooks/guard.py').resolve(),
                         (parent_claude_hooks / 'guard.py').resolve())
        self.assertEqual(dispatch_capacity_evidence.launch_scope(
            'claude', {**env, **claude})['quota_scope'],
            dispatch_capacity_evidence.launch_scope(
                'claude', {**env, 'CLAUDE_CONFIG_DIR': str(selected_claude)})['quota_scope'])
        self.assertFalse((claude_home / '.credentials.json').exists())

        opencode = prepare_worker_home(ROOT, 'opencode', 'stage', 'account-b-opencode', env=env)
        opencode_data = Path(opencode['XDG_DATA_HOME']) / 'opencode/auth.json'
        self.assertEqual(opencode_data.resolve(),
                         (selected_opencode_data / 'opencode/auth.json').resolve())
        self.assertEqual(json.loads((Path(opencode['OPENCODE_CONFIG_DIR']) / 'opencode.json').read_text())[
            'model'], 'provider/model-A')

        nested_opencode = prepare_worker_home(
            ROOT, 'opencode', 'review', 'nested-account-b-opencode', env={**env, **opencode})
        nested_auth = Path(nested_opencode['XDG_DATA_HOME']) / 'opencode/auth.json'
        self.assertEqual(nested_auth.resolve(),
                         (selected_opencode_data / 'opencode/auth.json').resolve())
        self.assertEqual(nested_opencode['DISPATCH_DEFAULTS_CONFIG'], str(defaults_path.resolve()))

        nested_claude = prepare_worker_home(
            ROOT, 'claude', 'review', 'nested-account-b-claude', env={**env, **claude})
        self.assertEqual(json.loads(
            (Path(nested_claude['CLAUDE_CONFIG_DIR']) / '.claude.json').read_text()),
            {'oauthAccount': selected_claude_account})
        self.assertEqual(nested_claude[SECURE_STORAGE_ENV], str(selected_store.resolve()))

        self.assertEqual({path: path.read_bytes() for path in original_bytes}, original_bytes)

    def test_explicit_missing_codex_account_does_not_fall_back_to_parent_auth(self):
        parent = self.base / 'parent-codex'
        selected = self.base / 'missing-codex-account'
        parent.mkdir()
        selected.mkdir()
        (parent / 'auth.json').write_text('{"account":"A"}', encoding='utf-8')
        (parent / 'config.toml').write_text('[user]\nvalue="A"\n', encoding='utf-8')
        defaults_path = self._account_defaults({'codex': str(selected)})
        env = {**self.env, 'HOME': str(self.base / 'home'), 'CODEX_HOME': str(parent),
               'DISPATCH_DEFAULTS_CONFIG': str(defaults_path)}
        values = prepare_worker_home(ROOT, 'codex', 'review', 'missing-account', env=env)
        self.assertFalse((Path(values['CODEX_HOME']) / 'auth.json').exists())
        self.assertEqual((Path(values['CODEX_HOME']) / 'config.toml').resolve(),
                         (parent / 'config.toml').resolve())

    def test_worker_account_path_expansion_uses_passed_home_and_launch_cwd(self):
        home = self.base / 'worker-home-root'
        launch_cwd = self.base / 'launch-cwd'
        home.mkdir()
        launch_cwd.mkdir()
        (home / 'account-codex').mkdir()
        (home / 'account-codex/auth.json').write_text('{"account":"tilde"}', encoding='utf-8')
        relative_account = launch_cwd / 'relative/claude-account'
        relative_account.mkdir(parents=True)
        (relative_account / '.claude.json').write_text(json.dumps({
            'oauthAccount': {'accountUuid': 'relative-account', 'organizationUuid': 'relative-org'}
        }), encoding='utf-8')
        defaults_path = self._account_defaults({
            'codex': '~/account-codex', 'claude': 'relative/claude-account',
        })
        env = {**self.env, 'HOME': str(home), 'DISPATCH_DEFAULTS_CONFIG': str(defaults_path)}
        with patch('pathlib.Path.cwd', return_value=launch_cwd):
            codex = prepare_worker_home(ROOT, 'codex', 'review', 'expanded-tilde', env=env)
            claude = prepare_worker_home(ROOT, 'claude', 'review', 'expanded-relative', env=env)
        self.assertEqual((Path(codex['CODEX_HOME']) / 'auth.json').resolve(),
                         (home / 'account-codex/auth.json').resolve())
        claude_config = Path(claude['CLAUDE_CONFIG_DIR']) / '.claude.json'
        self.assertEqual(claude['HEARTING_MODEL_CONFIG_HOME'],
                         str((home / '.claude').resolve()))
        self.assertEqual(json.loads(claude_config.read_text()), {
            'oauthAccount': {'accountUuid': 'relative-account', 'organizationUuid': 'relative-org'}
        })
        self.assertEqual(worker_runtime_home._builder._resolve_worker_account_path(
            'relative/claude-account', env=env, cwd=launch_cwd),
            (launch_cwd / 'relative/claude-account').resolve())

    def test_claude_guards_permissions_and_credentials_survive_catalog_removal(self):
        (self.source / 'hooks').mkdir()
        (self.source / 'hooks/user-guard.py').write_text('guard')
        settings = {'hooks': {'PreToolUse': [{'hooks': [{'command': 'user-guard'}]}]},
                    'permissions': {'deny': ['Read(secret)']}, 'env': {'USER_GUARD': '1'},
                    'enabledPlugins': {'plugin@market': True}, 'autoMemoryEnabled': True}
        (self.source / 'settings.json').write_text(json.dumps(settings))
        (self.source / '.credentials.json').write_text('{}')
        credential_bytes = (self.source / '.credentials.json').read_bytes()
        account = {'accountUuid': 'user-account', 'organizationUuid': 'user-org'}
        (self.source / '.claude.json').write_text(json.dumps({'oauthAccount': account, 'mcpServers': {'main': {}}}))
        self.env['CLAUDE_CONFIG_DIR'] = str(self.source)
        env = prepare_worker_home(ROOT, 'claude', 'support', 'tidy', env=self.env)
        self.assertEqual(env['HEARTING_MODEL_CONFIG_HOME'], str(self.source))
        self.assertEqual(env['HEARTING_MODEL_CONFIG_HOME'], str(self.source))
        home = Path(env['CLAUDE_CONFIG_DIR'])
        actual = json.loads((home / 'settings.json').read_text())
        for key in ('hooks', 'permissions', 'env'):
            self.assertEqual(actual[key], settings[key])
        self.assertEqual(actual['enabledPlugins'], {'plugin@market': False})
        self.assertFalse(actual['autoMemoryEnabled'])
        self.assertEqual(env[SECURE_STORAGE_ENV], str(self.source.resolve()))
        self.assertFalse((home / '.credentials.json').exists())
        self.assertEqual((self.source / '.credentials.json').read_bytes(), credential_bytes)
        self.assertEqual(json.loads((home / 'worker-home.json').read_text())[
                         'claude_secure_storage_dir'], str(self.source.resolve()))
        self.assertEqual((home / 'hooks/user-guard.py').resolve(), (self.source / 'hooks/user-guard.py').resolve())
        self.assertEqual(json.loads((home / '.claude.json').read_text()), {'oauthAccount': account})
        self.assertIn('--strict-mcp-config', claude_worker_arguments(env))
        self.assertNotIn('--bare', claude_worker_arguments(env))

    def test_claude_oauth_account_projection_uses_native_global_config_path(self):
        cli_home = self.base / 'cli-home'
        user_config = cli_home / '.claude'
        user_config.mkdir(parents=True)
        root_account = {'accountUuid': 'synthetic-home-account'}
        wrong_nested_account = {'accountUuid': 'synthetic-nested-account'}
        (cli_home / '.claude.json').write_text(json.dumps({
            'oauthAccount': root_account, 'mcpServers': {'private': {}}}))
        (user_config / '.claude.json').write_text(json.dumps({
            'oauthAccount': wrong_nested_account}))

        cases = (
            ('unset', {}, root_account),
            ('empty', {'CLAUDE_CONFIG_DIR': ''}, root_account),
        )
        for label, override, expected in cases:
            with self.subTest(config_dir=label):
                destination = self.base / f'account-projection-{label}'
                self.assertFalse(destination.exists())
                values = prepare_worker_home(
                    ROOT, 'claude', 'stage', 'account-projection-' + label,
                    env={**self.env, 'HOME': str(cli_home), **override},
                    destination=destination)
                self.assertEqual(json.loads(
                    (Path(values['CLAUDE_CONFIG_DIR']) / '.claude.json').read_text()),
                    {'oauthAccount': expected})

        empty_cwd = self.base / 'empty-config-cwd'
        empty_cwd.mkdir()
        empty_legacy_account = {'accountUuid': 'synthetic-empty-legacy-account'}
        (empty_cwd / '.config.json').write_text(json.dumps({
            'oauthAccount': empty_legacy_account}))
        empty_legacy_destination = self.base / 'account-projection-empty-legacy'
        with patch('pathlib.Path.cwd', return_value=empty_cwd):
            values = prepare_worker_home(
                ROOT, 'claude', 'stage', 'account-projection-empty-legacy',
                env={**self.env, 'HOME': str(cli_home), 'CLAUDE_CONFIG_DIR': ''},
                destination=empty_legacy_destination)
        self.assertEqual(json.loads(
            (Path(values['CLAUDE_CONFIG_DIR']) / '.claude.json').read_text()),
            {'oauthAccount': empty_legacy_account})

        custom = self.base / 'custom-config'
        custom.mkdir()
        custom_account = {'accountUuid': 'synthetic-custom-account'}
        (custom / '.claude.json').write_text(json.dumps({
            'oauthAccount': custom_account, 'mcpServers': {'private': {}}}))
        destination = self.base / 'account-projection-custom'
        self.assertFalse(destination.exists())
        values = prepare_worker_home(
            ROOT, 'claude', 'stage', 'account-projection-custom',
            env={**self.env, 'HOME': str(cli_home), 'CLAUDE_CONFIG_DIR': str(custom)},
            destination=destination)
        self.assertEqual(json.loads(
            (Path(values['CLAUDE_CONFIG_DIR']) / '.claude.json').read_text()),
            {'oauthAccount': custom_account})

        legacy_account = {'accountUuid': 'synthetic-custom-legacy-account'}
        (custom / '.config.json').write_text(json.dumps({
            'oauthAccount': legacy_account}))
        destination = self.base / 'account-projection-custom-legacy'
        values = prepare_worker_home(
            ROOT, 'claude', 'stage', 'account-projection-custom-legacy',
            env={**self.env, 'HOME': str(cli_home), 'CLAUDE_CONFIG_DIR': str(custom)},
            destination=destination)
        self.assertEqual(json.loads(
            (Path(values['CLAUDE_CONFIG_DIR']) / '.claude.json').read_text()),
            {'oauthAccount': legacy_account})

    def test_claude_unreadable_global_config_leaves_the_launch_unscoped(self):
        cli_home = self.base / 'rewriting-home'
        (cli_home / '.claude').mkdir(parents=True)
        for label, text in (('torn', '{"oauthAccount": '), ('non-object', '[]')):
            with self.subTest(config=label):
                (cli_home / '.claude.json').write_text(text)
                values = prepare_worker_home(
                    ROOT, 'claude', 'owner', 'unreadable-config-' + label,
                    env={**self.env, 'HOME': str(cli_home)})
                self.assertFalse((Path(values['CLAUDE_CONFIG_DIR']) / '.claude.json').exists())

    def test_claude_binding_covers_worker_types_and_cross_harness_descendants(self):
        source = self.base / 'main-claude-config'
        source.mkdir()
        (source / '.credentials.json').write_text('{"synthetic":"main"}')
        (source / 'settings.json').write_text(json.dumps({
            'permissions': {'deny': ['Read(secret)']}, 'env': {'USER_GUARD': '1'}}))
        env = {**self.env, 'HOME': str(self.base / 'user-home'),
               'CLAUDE_CONFIG_DIR': str(source)}
        expected = str(source.resolve())
        first = None
        for worker_type in ('owner', 'stage', 'review', 'support', 'frame'):
            with self.subTest(worker_type=worker_type):
                values = prepare_worker_home(
                    ROOT, 'claude', worker_type, 'all-types-' + worker_type, env=env)
                self.assertEqual(values[SECURE_STORAGE_ENV], expected)
                worker_config = Path(values['CLAUDE_CONFIG_DIR'])
                self.assertFalse((worker_config / '.credentials.json').exists())
                self.assertEqual(json.loads((worker_config / 'settings.json').read_text())[
                                 'permissions'], {'deny': ['Read(secret)']})
                if worker_type == 'owner':
                    first = values

        codex_source = self.base / 'cross-codex-config'
        codex_source.mkdir()
        for name, content in (('auth.json', '{}'), ('config.toml', '')):
            (codex_source / name).write_text(content)
        codex_before = {name: (codex_source / name).read_bytes()
                        for name in ('auth.json', 'config.toml')}
        codex_parent = {**env, **first, 'CODEX_HOME': str(codex_source)}
        codex = prepare_worker_home(ROOT, 'codex', 'review', 'claude-through-codex',
                                    env=codex_parent)
        self.assertNotIn(SECURE_STORAGE_ENV, codex)
        through_codex = {**codex_parent, **codex}
        nested_codex = prepare_worker_home(
            ROOT, 'claude', 'stage', 'nested-after-codex', env=through_codex)
        self.assertEqual(nested_codex[SECURE_STORAGE_ENV], expected)
        self.assertEqual({name: (codex_source / name).read_bytes()
                          for name in ('auth.json', 'config.toml')}, codex_before)

        opencode_source = self.base / 'cross-opencode-config'
        opencode_source.mkdir()
        opencode_config = opencode_source / 'opencode.json'
        opencode_config.write_text(json.dumps({'provider': {'fixture': {'name': 'test'}}}))
        opencode_before = opencode_config.read_bytes()
        opencode_parent = {**env, **first,
                           'OPENCODE_CONFIG_DIR': str(opencode_source),
                           'XDG_CONFIG_HOME': str(self.base / 'xdg-config')}
        opencode = prepare_worker_home(
            ROOT, 'opencode', 'review', 'claude-through-opencode', env=opencode_parent)
        self.assertNotIn(SECURE_STORAGE_ENV, opencode)
        through_opencode = {**opencode_parent, **opencode}
        nested_opencode = prepare_worker_home(
            ROOT, 'claude', 'stage', 'nested-after-opencode', env=through_opencode)
        self.assertEqual(nested_opencode[SECURE_STORAGE_ENV], expected)
        self.assertEqual(opencode_config.read_bytes(), opencode_before)

    def test_claude_secure_storage_override_values_and_config_fallback_are_preserved(self):
        home = self.base / 'worker-home'
        config = self.base / 'main-config'
        env = {**self.env, 'HOME': str(self.base / 'user-home'),
               'CLAUDE_CONFIG_DIR': str(config)}
        for value, expected in (
                ('', ''),
                (str(self.base / 'custom-auth'), str(self.base / 'custom-auth')),
                ('relative-auth', str((Path.cwd() / 'relative-auth').resolve()))):
            with self.subTest(value=value):
                result = prepare_worker_home(
                    ROOT, 'claude', 'stage', 'override-' + repr(value),
                    env={**env, SECURE_STORAGE_ENV: value},
                    destination=home / ('empty' if value == '' else str(abs(hash(value)))))
                self.assertEqual(result[SECURE_STORAGE_ENV], expected)

        default_env = {**self.env, 'HOME': str(self.base / 'alternate-home')}
        default_env.pop('CLAUDE_CONFIG_DIR', None)
        default = prepare_worker_home(ROOT, 'claude', 'review', 'default-auth',
                                      env=default_env, destination=home / 'default')
        self.assertEqual(default[SECURE_STORAGE_ENV], '')

        missing_custom = self.base / 'missing-account-config'
        custom = prepare_worker_home(
            ROOT, 'claude', 'support', 'missing-custom-auth',
            env={**self.env, 'HOME': str(self.base / 'alternate-home'),
                 'CLAUDE_CONFIG_DIR': str(missing_custom)},
            destination=home / 'missing-custom')
        self.assertEqual(custom[SECURE_STORAGE_ENV], str(missing_custom.resolve()))
        self.assertFalse((Path(custom['CLAUDE_CONFIG_DIR']) / '.credentials.json').exists())

    def test_claude_settings_env_precedence_is_normalized_and_nested_store_is_stable(self):
        config = self.base / 'main-config'
        config.mkdir()
        (config / 'settings.json').write_text(json.dumps({
            'env': {SECURE_STORAGE_ENV: 'settings-auth', 'USER_GUARD': '1'},
            'permissions': {'deny': ['Read(secret)']}}))
        env = {**self.env, 'CLAUDE_CONFIG_DIR': str(config),
               SECURE_STORAGE_ENV: str(self.base / 'inherited-auth')}
        first = prepare_worker_home(ROOT, 'claude', 'stage', 'settings-precedence', env=env)
        binding = str((Path.cwd() / 'settings-auth').resolve())
        self.assertEqual(first[SECURE_STORAGE_ENV], binding)
        first_home = Path(first['CLAUDE_CONFIG_DIR'])
        generated = json.loads((first_home / 'settings.json').read_text())
        self.assertEqual(generated['env'][SECURE_STORAGE_ENV], binding)
        self.assertEqual(generated['env']['USER_GUARD'], '1')
        self.assertEqual(generated['permissions'], {'deny': ['Read(secret)']})

        child = prepare_worker_home(
            ROOT, 'claude', 'review', 'nested-settings-store', env={**env, **first})
        self.assertNotEqual(child['CLAUDE_CONFIG_DIR'], first['CLAUDE_CONFIG_DIR'])
        self.assertEqual(child[SECURE_STORAGE_ENV], binding)

    def test_claude_legacy_links_and_split_candidates_use_main_store(self):
        canonical = self.base / 'legacy-canonical'
        canonical.mkdir()
        (canonical / '.credentials.json').write_text('{"synthetic":"canonical"}')
        legacy_link_home = self.base / 'legacy-link-home'
        legacy_link_home.mkdir()
        (legacy_link_home / 'worker-home.json').write_text('{"harness":"claude"}')
        (legacy_link_home / '.credentials.json').symlink_to(canonical / '.credentials.json')
        linked_destination = self.base / 'new-linked-home'
        linked_destination.mkdir()
        (linked_destination / '.credentials.json').symlink_to(
            canonical / '.credentials.json')
        linked = prepare_worker_home(
            ROOT, 'claude', 'stage', 'legacy-intact-link',
            env={**self.env, 'CLAUDE_CONFIG_DIR': str(legacy_link_home)},
            destination=linked_destination)
        self.assertEqual(linked[SECURE_STORAGE_ENV], str(canonical.resolve()))
        self.assertTrue((legacy_link_home / '.credentials.json').is_symlink())
        self.assertFalse((linked_destination / '.credentials.json').exists())

        legacy_regular_home = self.base / 'legacy-regular-home'
        legacy_regular_home.mkdir()
        candidate = legacy_regular_home / '.credentials.json'
        candidate.write_bytes(b'{"synthetic":"opaque-candidate"}')
        before = candidate.read_bytes()
        (legacy_regular_home / 'worker-home.json').write_text('{"harness":"claude"}')
        (legacy_regular_home / 'settings.json').write_text(json.dumps({
            'permissions': {'deny': ['Read(secret)']},
            'env': {'USER_GUARD': '1'}}))
        legacy_main_home = self.base / 'legacy-main-home'
        (legacy_main_home / '.claude').mkdir(parents=True)
        main_candidate = legacy_main_home / '.claude' / '.credentials.json'
        main_candidate.write_bytes(b'{"synthetic":"main-current"}')
        main_before = main_candidate.read_bytes()
        regular_destination = self.base / 'new-regular-home'
        self.assertFalse(regular_destination.exists())
        env = {**self.env, 'HOME': str(legacy_main_home),
               'CLAUDE_CONFIG_DIR': str(legacy_regular_home)}
        regular = prepare_worker_home(
            ROOT, 'claude', 'stage', 'legacy-opaque-candidate', env=env,
            destination=regular_destination)
        # The split file is not this child's auth source: without a paired
        # legacy launch environment, the builder's HOME/.claude main is used.
        self.assertEqual(regular[SECURE_STORAGE_ENV], '')
        self.assertNotEqual(regular['CLAUDE_CONFIG_DIR'], str(legacy_regular_home))
        self.assertFalse((regular_destination / '.credentials.json').exists())
        self.assertEqual(candidate.read_bytes(), before)
        self.assertEqual(main_candidate.read_bytes(), main_before)
        self.assertEqual(json.loads((regular_destination / 'settings.json').read_text())[
                         'permissions'], {'deny': ['Read(secret)']})
        self.assertEqual(json.loads((regular_destination / 'worker-home.json').read_text())[
                         'claude_secure_storage_dir'], '')

        # A legacy parent's own launch environment names its source main.
        custom_main = self.base / 'legacy-custom-main'
        custom_main.mkdir()
        for label, source, expected in (
                ('default', legacy_main_home / '.claude', ''),
                ('custom', custom_main, str(custom_main.resolve()))):
            with self.subTest(paired_source=label):
                paired = prepare_worker_home(
                    ROOT, 'claude', 'stage', 'legacy-paired-' + label,
                    env={**env, 'HEARTING_WORKER_HOME': str(legacy_regular_home),
                         'HEARTING_MODEL_CONFIG_HOME': str(source)},
                    destination=self.base / ('new-paired-home-' + label))
                self.assertEqual(paired[SECURE_STORAGE_ENV], expected)
                self.assertEqual(candidate.read_bytes(), before)
                self.assertEqual(main_candidate.read_bytes(), main_before)
                self.assertFalse((custom_main / '.credentials.json').exists())

        occupied_destination = self.base / 'occupied-regular-home'
        occupied_destination.mkdir()
        destination_candidate = occupied_destination / '.credentials.json'
        destination_candidate.write_bytes(b'{"synthetic":"destination-candidate"}')
        destination_before = destination_candidate.read_bytes()
        occupied = prepare_worker_home(
            ROOT, 'claude', 'stage', 'legacy-opaque-candidate-occupied', env=env,
            destination=occupied_destination)
        self.assertEqual(occupied[SECURE_STORAGE_ENV], '')
        self.assertEqual(destination_candidate.read_bytes(), destination_before)
        self.assertEqual(candidate.read_bytes(), before)
        self.assertEqual(main_candidate.read_bytes(), main_before)

        nested_destination = self.base / 'nested-regular-home'
        nested = prepare_worker_home(
            ROOT, 'claude', 'review', 'nested-legacy-opaque-candidate',
            env={**env, **regular}, destination=nested_destination)
        self.assertEqual(nested[SECURE_STORAGE_ENV], '')
        self.assertNotEqual(nested['CLAUDE_CONFIG_DIR'], regular['CLAUDE_CONFIG_DIR'])
        self.assertFalse((nested_destination / '.credentials.json').exists())
        self.assertEqual(candidate.read_bytes(), before)
        self.assertEqual(main_candidate.read_bytes(), main_before)

        empty_legacy_home = self.base / 'legacy-without-credentials'
        empty_legacy_home.mkdir()
        (empty_legacy_home / 'worker-home.json').write_text('{"harness":"claude"}')
        empty_destination = self.base / 'new-home-without-credentials'
        empty = prepare_worker_home(
            ROOT, 'claude', 'stage', 'legacy-no-credential',
            env={**env, 'CLAUDE_CONFIG_DIR': str(empty_legacy_home)},
            destination=empty_destination)
        self.assertEqual(empty[SECURE_STORAGE_ENV], '')
        self.assertFalse((empty_legacy_home / '.credentials.json').exists())
        self.assertFalse((empty_destination / '.credentials.json').exists())
        self.assertEqual(main_candidate.read_bytes(), main_before)

    def test_manifest_records_the_identity_retention_needs_to_bind_a_home(self):
        (self.source / 'settings.json').write_text('{}')
        env = {**self.env, 'CLAUDE_CONFIG_DIR': str(self.source)}
        built = prepare_worker_home(ROOT, 'claude', 'stage', 'att-manifest', env=env)
        home = Path(built['CLAUDE_CONFIG_DIR'])
        manifest = json.loads((home / 'worker-home.json').read_text())
        key = worker_runtime_home._builder.worker_home_key(str(ROOT.resolve()), 'claude', 'stage', 'att-manifest')
        self.assertEqual(home.name, key)
        self.assertEqual((manifest['schema'], manifest['identity'], manifest['key'], manifest['destination']),
                         (2, 'att-manifest', key, False))
        self.assertEqual({'harness', 'worker_type', 'source', 'profile'} - set(manifest), set())
        shared = self.base / 'shared-home'
        built = prepare_worker_home(ROOT, 'claude', 'stage', 'att-shared', env=env, destination=shared)
        manifest = json.loads((shared / 'worker-home.json').read_text())
        self.assertTrue(manifest['destination'])
        self.assertEqual(manifest['identity'], 'att-shared')

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
        self.env['XDG_CACHE_HOME'] = str(self.source / 'cache')
        env = prepare_worker_home(ROOT, 'opencode', 'review', 'review', env=self.env)
        self.assertEqual(env['HEARTING_MODEL_CONFIG_HOME'], str(self.source))
        self.assertEqual(env['HEARTING_OPENCODE_MODELS_CACHE'], str(self.source / 'cache' / 'opencode'))
        self.assertEqual(env['HEARTING_MODEL_CONFIG_HOME'], str(self.source))
        home = Path(env['OPENCODE_CONFIG_DIR'])
        actual = json.loads((home / 'opencode.json').read_text())
        self.assertEqual(actual['permission']['bash'], config['permission']['bash'])
        self.assertEqual(actual['provider'], config['provider'])
        self.assertEqual(actual['instructions'], [])
        self.assertEqual(actual['permission']['skill'], {'*': 'deny'})
        self.assertEqual((home / 'plugins').resolve(), (self.source / 'plugins').resolve())
        self.assertEqual(env['OPENCODE_DISABLE_CLAUDE_CODE_PROMPT'], '1')

    def test_opencode_v2_worker_uses_shared_service_without_db_or_auth_links(self):
        data_home = self.base / 'v2-data'
        data_dir = data_home / 'opencode'
        data_dir.mkdir(parents=True)
        with sqlite3.connect(data_dir / 'opencode.db') as connection:
            connection.execute('CREATE TABLE credential (id TEXT PRIMARY KEY, integration_id TEXT, '
                               'label TEXT, value TEXT, active INTEGER, time_created INTEGER, time_updated INTEGER)')
        config_source = self.base / 'v2-config'
        config_source.mkdir()
        (config_source / 'opencode.json').write_text(json.dumps({
            'permissions': [{'action': 'shell', 'resource': '*', 'effect': 'ask'}],
            'plugins': ['user-plugin'],
            'mcp': {'servers': {'user-server': {'type': 'local', 'command': ['tool']}}},
        }))
        env = {**self.env, 'HOME': str(self.base), 'XDG_DATA_HOME': str(data_home),
               'XDG_CONFIG_HOME': str(self.base / 'config'),
               'XDG_STATE_HOME': str(self.base / 'state'),
               'XDG_CACHE_HOME': str(self.base / 'cache'),
               'OPENCODE_CONFIG_DIR': str(config_source)}
        with patch.dict(os.environ, {'OPENCODE_RUNTIME_VERSION': '2.0.32'}):
            result = prepare_worker_home(ROOT, 'opencode', 'stage', 'v2-worker', env=env,
                                         destination=self.base / 'v2-worker-home')
        self.assertNotIn('XDG_DATA_HOME', result)
        self.assertNotIn('OPENCODE_CONFIG_DIR', result)
        home = Path(result['HEARTING_WORKER_HOME'])
        projected = json.loads((home / 'opencode.json').read_text())
        self.assertIn('permissions', projected)
        self.assertNotIn('permission', projected)
        self.assertEqual(projected['plugins'], [])
        self.assertEqual(projected['mcp'], {'servers': {}})
        self.assertFalse((home / 'runtime/data/opencode/auth.json').exists())
        self.assertFalse((home / 'runtime/data/opencode/opencode.db').exists())

    def test_opencode_home_uses_the_launch_observation_instead_of_process_probe(self):
        import opencode_runtime
        data_home = self.base / 'observed-v2-data'
        data_home.mkdir()
        config_source = self.base / 'observed-v2-config'
        config_source.mkdir()
        (config_source / 'opencode.json').write_text('{}', encoding='utf-8')
        env = {**self.env, 'HOME': str(self.base),
               'OPENCODE_CONFIG_DIR': str(config_source),
               'XDG_DATA_HOME': str(data_home),
               'OPENCODE_RUNTIME_VERSION': '2.0.32'}
        observation = opencode_runtime.OpenCodeLaunchObservation(
            binary='/usr/bin/opencode', version=(2, 0, 32), generation='v2',
            cwd=str(Path.cwd().resolve()))
        with patch.dict(os.environ, {'OPENCODE_RUNTIME_VERSION': '1.19.0'}), \
             patch.object(opencode_runtime, 'is_v2', side_effect=AssertionError('re-probed')):
            result = prepare_worker_home(
                ROOT, 'opencode', 'stage', 'observed-v2', env=env,
                destination=self.base / 'observed-v2-home',
                runtime_observation=observation)
        self.assertNotIn('XDG_DATA_HOME', result)
        self.assertNotIn('OPENCODE_CONFIG_DIR', result)
        self.assertEqual((data_home / 'opencode').exists(), False)

    def test_opencode_all_worker_types_override_snapshot_without_changing_user_config(self):
        config = {'snapshot': True, 'permission': {'bash': {'secret *': 'deny'}}}
        source = self.source / 'opencode.json'
        source.write_text(json.dumps(config))
        self.env.update(OPENCODE_CONFIG_DIR=str(self.source),
                        OPENCODE_CONFIG_CONTENT=json.dumps({'snapshot': True, 'model': 'test/model'}))
        before = source.read_bytes()
        for typ in ('owner', 'stage', 'review', 'support', 'frame'):
            env = prepare_worker_home(ROOT, 'opencode', typ, 'snapshot-' + typ, env=self.env)
            self.assertNotIn(SECURE_STORAGE_ENV, env)
            actual = json.loads((Path(env['OPENCODE_CONFIG_DIR']) / 'opencode.json').read_text())
            self.assertFalse(actual['snapshot'])
            inline = json.loads(env['OPENCODE_CONFIG_CONTENT'])
            self.assertFalse(inline['snapshot'])
            self.assertEqual(inline['model'], 'test/model')
            self.assertEqual(actual['permission']['bash'], config['permission']['bash'])
        self.assertEqual(source.read_bytes(), before)

    def test_profile_specialization_uses_same_guard_preserving_home(self):
        (self.source / 'settings.json').write_text('{"permissions":{"deny":["Read(secret)"]}}')
        self.env['CLAUDE_CONFIG_DIR'] = str(self.source)
        env = prepare_worker_home(ROOT, 'claude', 'stage', 'specialized', env=self.env, profile='code-report')
        home = Path(env['CLAUDE_CONFIG_DIR'])
        self.assertIn('profiles/code-report.yaml', (home / 'CLAUDE.md').read_text())
        self.assertEqual(json.loads((home / 'settings.json').read_text())['permissions']['deny'], ['Read(secret)'])

    def test_opencode_isolation_keeps_the_user_inventory_through_descendants(self):
        config = self.base / 'user-config'
        inventory = config / 'hearting/compute-hosts.yaml'
        inventory.parent.mkdir(parents=True)
        inventory.write_text(f'schema_version: 1\nrun_root: {self.base}/runs\nhosts:\n  fixture:\n    ssh_host: local\n')
        before = inventory.read_bytes()
        env = {**self.env, 'HOME': str(self.source), 'XDG_CONFIG_HOME': str(config)}
        owner = prepare_worker_home(ROOT, 'opencode', 'owner', 'lab-owner', env=env)
        self.assertNotEqual(owner['XDG_CONFIG_HOME'], str(config))
        self.assertEqual(owner['COMPUTE_HOSTS_CONFIG'], str(inventory))
        child = prepare_worker_home(ROOT, 'opencode', 'review', 'lab-child', env={**env, **owner})
        for values in (owner, child):
            # Run the real inventory loader in the actual masked environment,
            # rather than only checking a projected environment string.
            result = subprocess.run([sys.executable, '-c',
                                     'import importlib.util; '
                                     f's=importlib.util.spec_from_file_location("compute", {str(ROOT / "utilities/compute-hosts.py")!r}); '
                                     'm=importlib.util.module_from_spec(s); s.loader.exec_module(m); '
                                     'print(m.config_path()); print(m.load_config()["run_root"])'],
                                    env={**os.environ, **env, **values},
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(str(inventory), result.stdout)
            self.assertFalse((Path(values['XDG_CONFIG_HOME']) / 'hearting').exists())
        self.assertEqual(inventory.read_bytes(), before)

    def test_opencode_preserves_an_explicit_inventory_location(self):
        inventory = self.base / 'operator/inventory.yaml'
        values = prepare_worker_home(ROOT, 'opencode', 'owner', 'explicit-inventory',
                                     env={**self.env, 'COMPUTE_HOSTS_CONFIG': str(inventory)})
        self.assertEqual(values['COMPUTE_HOSTS_CONFIG'], str(inventory))

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

    def test_concurrent_home_builders_reuse_the_same_link_in_every_harness(self):
        # Force both creators past the missing-path observation before either
        # symlink is published, rather than relying on a lucky thread schedule.
        original = Path.symlink_to
        for harness, filename, variable in (
                ('codex', 'AGENTS.md', 'CODEX_HOME'),
                ('claude', 'CLAUDE.md', 'CLAUDE_CONFIG_DIR'),
                ('opencode', 'AGENTS.md', 'OPENCODE_CONFIG_DIR')):
            with self.subTest(harness=harness):
                barrier = threading.Barrier(2)
                env = {**self.env, variable: str(self.source)}
                def synchronized(path, *args, **kwargs):
                    if path.name == filename:
                        barrier.wait(timeout=5)
                    return original(path, *args, **kwargs)
                def build():
                    return prepare_worker_home(ROOT, harness, 'stage', harness, env=env)
                with patch.object(Path, 'symlink_to', synchronized), ThreadPoolExecutor(2) as pool:
                    futures = [pool.submit(build) for _ in range(2)]
                    results = [future.result(timeout=10) for future in futures]
                self.assertEqual(results[0], results[1])
                home = Path(results[0][variable])
                self.assertEqual((home / filename).read_bytes(),
                                 (ROOT / f'profiles/templates/bootstrap-{harness}.md').read_bytes())
                self.assertEqual(json.loads((home / 'worker-home.json').read_text())['harness'], harness)

    def test_generated_files_and_profile_refresh_publish_complete_bytes(self):
        settings = {'permissions': {'deny': ['Read(secret)']}}
        (self.source / 'settings.json').write_text(json.dumps(settings))
        env = {**self.env, 'CLAUDE_CONFIG_DIR': str(self.source)}
        first = prepare_worker_home(ROOT, 'claude', 'stage', 'profile-refresh',
                                    env=env, profile='code-report')
        home = Path(first['CLAUDE_CONFIG_DIR'])
        before = {p.name: p.read_bytes() for p in home.iterdir() if p.is_file()}
        bootstrap_inode = (home / 'CLAUDE.md').stat().st_ino
        original = os.replace
        publications = []
        def checked_replace(source, destination):
            destination = Path(destination)
            self.assertEqual(destination.read_bytes(), before[destination.name])
            if destination.suffix == '.json':
                json.loads(Path(source).read_text())
            self.assertEqual(Path(source).stat().st_mode & 0o777, 0o600)
            publications.append(destination.name)
            return original(source, destination)
        with patch.object(worker_runtime_home._builder.os, 'replace', checked_replace), ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(prepare_worker_home, ROOT, 'claude', 'stage',
                                   'profile-refresh', env=env, profile='code-report') for _ in range(2)]
            self.assertEqual([f.result(timeout=10) for f in futures], [first, first])
        self.assertEqual(publications.count('CLAUDE.md'), 0)
        self.assertEqual((home / 'CLAUDE.md').stat().st_ino, bootstrap_inode)
        self.assertEqual(publications.count('settings.json'), 2)
        self.assertEqual(publications.count('worker-home.json'), 2)

    def test_concurrent_profile_bootstrap_publication_is_idempotent(self):
        builder = worker_runtime_home._builder
        env = {**self.env, 'CLAUDE_CONFIG_DIR': str(self.source)}
        original = os.link
        barrier = threading.Barrier(2)
        def synchronized(source, destination, *args, **kwargs):
            if Path(destination).name == 'CLAUDE.md':
                barrier.wait(timeout=5)
            return original(source, destination, *args, **kwargs)
        with patch.object(builder.os, 'link', synchronized), ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(prepare_worker_home, ROOT, 'claude', 'stage',
                                   'new-profile', env=env, profile='code-report') for _ in range(2)]
            results = [f.result(timeout=10) for f in futures]
        self.assertEqual(results[0], results[1])
        self.assertIn('profiles/code-report.yaml',
                      (Path(results[0]['CLAUDE_CONFIG_DIR']) / 'CLAUDE.md').read_text())

    def test_profile_preserves_a_foreign_regular_bootstrap(self):
        home = self.base / 'foreign-home'
        home.mkdir()
        bootstrap = home / 'CLAUDE.md'
        bootstrap.write_text('foreign instructions')
        with self.assertRaisesRegex(ValueError, 'worker home collision'):
            prepare_worker_home(ROOT, 'claude', 'stage', 'foreign-profile',
                                env={**self.env, 'CLAUDE_CONFIG_DIR': str(self.source)},
                                destination=home, profile='code-report')
        self.assertEqual(bootstrap.read_text(), 'foreign instructions')

    def test_link_refresh_is_atomic_and_foreign_file_is_preserved(self):
        builder = worker_runtime_home._builder
        old, new, target = self.base / 'old', self.base / 'new', self.base / 'link'
        old.write_text('old'); new.write_text('new')
        builder.publish_symlink(old, target)
        original = os.replace
        def checked_replace(source, destination):
            self.assertEqual(target.read_text(), 'old')
            return original(source, destination)
        with patch.object(builder.os, 'replace', checked_replace):
            builder.publish_symlink(new, target)
        self.assertEqual(target.read_text(), 'new')
        with patch.object(Path, 'unlink', side_effect=AssertionError('matching link removed')):
            builder.publish_symlink(new, target)
        target.unlink(); target.write_text('foreign')
        with self.assertRaisesRegex(ValueError, 'worker home collision'):
            builder.publish_symlink(new, target)
        self.assertEqual(target.read_text(), 'foreign')


if __name__ == '__main__':
    unittest.main()
