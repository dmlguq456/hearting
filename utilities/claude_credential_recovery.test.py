"""Safe selection behavior for legacy Claude worker credential layouts."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
from pathlib import Path
import tempfile
import unittest

from claude_credential_recovery import SECURE_STORAGE_ENV, resolve_storage_binding


class ClaudeCredentialRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cwd = self.root / 'caller'
        self.cwd.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_explicit_values_keep_native_empty_and_relative_path_meanings(self):
        config = self.root / 'config'
        self.assertEqual(resolve_storage_binding(
            {SECURE_STORAGE_ENV: ''}, config, cwd=self.cwd), '')
        self.assertEqual(resolve_storage_binding(
            {SECURE_STORAGE_ENV: 'relative-auth'}, config, cwd=self.cwd),
            str((self.cwd / 'relative-auth').resolve()))
        self.assertEqual(resolve_storage_binding(
            {SECURE_STORAGE_ENV: str(self.root / 'custom-auth')}, config, cwd=self.cwd),
            str(self.root / 'custom-auth'))

    def test_settings_override_inherited_value_and_empty_is_not_dropped(self):
        config = self.root / 'config'
        self.assertEqual(resolve_storage_binding(
            {SECURE_STORAGE_ENV: str(self.root / 'inherited')}, config,
            settings_env={SECURE_STORAGE_ENV: ''}, cwd=self.cwd), '')
        self.assertEqual(resolve_storage_binding(
            {}, config, settings_env={SECURE_STORAGE_ENV: 'settings-auth'}, cwd=self.cwd),
            str((self.cwd / 'settings-auth').resolve()))

    def test_default_config_uses_native_empty_binding_and_custom_config_is_pinned(self):
        home = self.root / 'user'
        self.assertEqual(resolve_storage_binding(
            {'HOME': str(home)}, home / '.claude', cwd=self.cwd), '')
        custom = self.root / 'account-b'
        self.assertEqual(resolve_storage_binding(
            {'HOME': str(home)}, custom, cwd=self.cwd), str(custom.resolve()))

    def test_intact_legacy_link_reconnects_to_its_exact_store(self):
        store = self.root / 'store'
        store.mkdir()
        (store / '.credentials.json').write_text('{"test":"synthetic"}')
        worker = self.root / 'legacy-worker'
        worker.mkdir()
        (worker / 'worker-home.json').write_text(json.dumps({'harness': 'claude'}))
        (worker / '.credentials.json').symlink_to(store / '.credentials.json')
        self.assertEqual(resolve_storage_binding(
            {'HOME': str(self.root / 'user')}, worker, cwd=self.cwd), str(store.resolve()))

    def test_intact_link_to_default_home_keeps_native_empty_binding(self):
        home = self.root / 'user'
        (home / '.claude').mkdir(parents=True)
        (home / '.claude' / '.credentials.json').write_text('{"test":"synthetic"}')
        worker = self.root / 'legacy-worker'
        worker.mkdir()
        (worker / 'worker-home.json').write_text(json.dumps({'harness': 'claude'}))
        (worker / '.credentials.json').symlink_to(home / '.claude' / '.credentials.json')
        self.assertEqual(resolve_storage_binding(
            {'HOME': str(home)}, worker, cwd=self.cwd), '')

    def test_split_legacy_candidate_is_preserved_and_new_launch_uses_main(self):
        home = self.root / 'user'
        default_main = home / '.claude'
        default_main.mkdir(parents=True)
        main_file = default_main / '.credentials.json'
        main_file.write_bytes(b'{"synthetic":"new-main"}')
        custom_main = self.root / 'custom-main'
        custom_main.mkdir()
        worker = self.root / 'legacy-worker'
        worker.mkdir()
        (worker / 'worker-home.json').write_text(json.dumps({'harness': 'claude'}))
        candidate = worker / '.credentials.json'
        candidate.write_bytes(b'{"synthetic":"opaque-worker-token"}')
        empty_worker = self.root / 'legacy-worker-without-credentials'
        empty_worker.mkdir()
        (empty_worker / 'worker-home.json').write_text(json.dumps({'harness': 'claude'}))
        before = candidate.read_bytes()
        main_before = main_file.read_bytes()

        def paired(config, source):
            return {'HOME': str(home), 'HEARTING_WORKER_HOME': str(config),
                    'HEARTING_MODEL_CONFIG_HOME': str(source)}

        for config in (worker, empty_worker):
            with self.subTest(config=config.name):
                # Without the builder's paired launch environment, follow the
                # legacy builder's HOME/.claude fallback.
                self.assertEqual(resolve_storage_binding(
                    {'HOME': str(home)}, config, cwd=self.cwd), '')
                self.assertEqual(resolve_storage_binding(
                    paired(config, default_main), config, cwd=self.cwd), '')
                self.assertEqual(resolve_storage_binding(
                    paired(config, custom_main), config, cwd=self.cwd),
                    str(custom_main.resolve()))
                # An environment paired with another worker home is not this
                # worker's lineage.
                self.assertEqual(resolve_storage_binding(
                    paired(self.root / 'other-worker', custom_main), config,
                    cwd=self.cwd), '')
        self.assertEqual(candidate.read_bytes(), before)
        self.assertEqual(main_file.read_bytes(), main_before)
        self.assertFalse((empty_worker / '.credentials.json').exists())

    def test_legacy_source_worker_resolves_its_own_lineage_once(self):
        home = self.root / 'user'
        store = self.root / 'store'
        store.mkdir()
        (store / '.credentials.json').write_text('{"test":"synthetic"}')
        linked_parent = self.root / 'linked-parent'
        linked_parent.mkdir()
        (linked_parent / 'worker-home.json').write_text(json.dumps({'harness': 'claude'}))
        (linked_parent / '.credentials.json').symlink_to(store / '.credentials.json')
        split_parent = self.root / 'split-parent'
        split_parent.mkdir()
        (split_parent / 'worker-home.json').write_text(json.dumps({'harness': 'claude'}))
        (split_parent / '.credentials.json').write_text('{"synthetic":"split"}')
        worker = self.root / 'legacy-worker'
        worker.mkdir()
        (worker / 'worker-home.json').write_text(json.dumps({'harness': 'claude'}))
        (worker / '.credentials.json').write_text('{"synthetic":"split-child"}')
        for parent, expected in ((linked_parent, str(store.resolve())), (split_parent, '')):
            with self.subTest(parent=parent.name):
                self.assertEqual(resolve_storage_binding(
                    {'HOME': str(home), 'HEARTING_WORKER_HOME': str(worker),
                     'HEARTING_MODEL_CONFIG_HOME': str(parent)}, worker,
                    cwd=self.cwd), expected)

    def test_new_worker_metadata_propagates_binding_across_nested_launches(self):
        worker = self.root / 'worker'
        worker.mkdir()
        (worker / 'worker-home.json').write_text(json.dumps({
            'harness': 'claude', 'claude_secure_storage_dir': str(self.root / 'store')}))
        self.assertEqual(resolve_storage_binding(
            {'HOME': str(self.root / 'user')}, worker, cwd=self.cwd),
            str(self.root / 'store'))


if __name__ == '__main__':
    unittest.main()
