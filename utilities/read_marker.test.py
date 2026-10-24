#!/usr/bin/env python3
"""Portable read-marker writers: CLI failures versus non-blocking read hooks."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ReadMarkerStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.art = self.repo / '.agent_reports'
        (self.art / 'spec').mkdir(parents=True)
        (self.repo / 'core').mkdir()
        self.spec = self.art / 'spec/prd.md'
        self.core = self.repo / 'core/CORE.md'
        self.spec.write_text('spec fixture\n')
        self.core.write_text('core fixture\n')
        self.home = Path(self.tmp.name) / 'marker-home'
        self.home.mkdir()
        self.state_root = Path(self.tmp.name) / 'hearting-state'
        self.marker_dir = self.state_root / 'spec-grounding'
        self.env = {'PATH': os.environ['PATH'], 'HOME': self.tmp.name,
                    'AGENT_HOME': str(self.home),
                    'HARNESS_STATE_ROOT': str(self.state_root),
                    'AGENT_ARTIFACT_ROOT': str(self.art),
                    'PYTHONDONTWRITEBYTECODE': '1'}

    def readonly(self, cmd):
        if not shutil.which('bwrap'):
            self.skipTest('bwrap required for a real read-only mount')
        return ['bwrap', '--ro-bind', '/', '/', '--dev', '/dev',
                '--die-with-parent', '--', *cmd]

    def run_marker(self, kind, *, hook=False, readonly=False, wrapper=False):
        directory = 'adapters/claude/hooks' if wrapper else 'hooks'
        script = ROOT / directory / f'{kind}-read-marker.sh'
        env = dict(self.env)
        if wrapper:
            env['AGENT_HOME'] = str(ROOT)
        file = self.spec if kind == 'spec' else self.core
        cmd = [str(script)]
        data = None
        if hook:
            data = json.dumps({'tool_input': {'file_path': str(file)},
                               'session_id': 'marker-fixture'})
        else:
            cmd += ['--file', str(file), '--session', 'marker-fixture']
        if readonly:
            cmd = self.readonly(cmd)
        return subprocess.run(cmd, input=data, env=env, text=True, capture_output=True)

    def test_cli_readonly_failure_is_nonzero(self):
        for kind in ('spec',):
            with self.subTest(kind=kind):
                result = self.run_marker(kind, readonly=True)
                self.assertIn('Read-only file system', result.stderr)
                self.assertNotEqual(result.returncode, 0, result.stderr)

    def test_hook_readonly_failure_is_nonblocking_and_diagnosed(self):
        for kind in ('spec',):
            with self.subTest(kind=kind):
                result = self.run_marker(kind, hook=True, readonly=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'{kind}-read-marker: marker-write-failed', result.stderr)
                self.assertEqual(result.stdout, '')

    def test_existing_directory_does_not_hide_marker_file_failure(self):
        for kind in ('spec',):
            self.marker_dir.mkdir(parents=True)
            for hook in (False, True):
                with self.subTest(kind=kind, hook=hook):
                    result = self.run_marker(kind, hook=hook, readonly=True)
                    self.assertIn('Read-only file system', result.stderr)
                    self.assertIn('marker-write-failed (read not recorded)', result.stderr)
                    self.assertEqual(result.returncode == 0, hook, result.stderr)

    def test_exact_spec_directory_grant_allows_marker_but_not_home_write(self):
        marker_dir = self.marker_dir
        marker_dir.mkdir(parents=True)
        cmd = self.readonly(['sh', '-c',
            '"$1" --file "$2" --session marker-fixture || exit $?; '
            'if touch "$3/unrelated"; then exit 99; fi',
            'marker-check', str(ROOT/'hooks/spec-read-marker.sh'),
            str(self.spec), str(self.home)])
        separator = cmd.index('--')
        cmd[separator:separator] = ['--bind', str(marker_dir), str(marker_dir)]
        result = subprocess.run(cmd, env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Read-only file system', result.stderr)
        markers = list(marker_dir.iterdir())
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0].read_text().strip(), str(int(self.spec.stat().st_mtime)))
        self.assertFalse((self.home/'unrelated').exists())

    def test_success_records_real_read_and_unrelated_read_is_noop(self):
        for kind, source in (('spec', self.spec),):
            result = self.run_marker(kind)
            self.assertEqual(result.returncode, 0, result.stderr)
            markers = list(self.marker_dir.iterdir())
            self.assertEqual(len(markers), 1)
            self.assertIn(str(int(source.stat().st_mtime)), markers[0].read_text())
        result = subprocess.run([str(ROOT/'hooks/spec-read-marker.sh'), '--file', str(self.core)],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(list(self.marker_dir.iterdir())), 1)
        self.assertEqual(self.marker_dir.stat().st_mode & 0o777, 0o700)

    def test_preflights_preserve_spec_cli_failures(self):
        env = dict(self.env, AGENT_HOME=str(ROOT))
        for adapter in ('codex', 'opencode'):
            for file in (self.spec,):
                with self.subTest(adapter=adapter, file=file.name):
                    cmd = self.readonly([str(ROOT/f'adapters/{adapter}/bin/preflight.sh'),
                                         'read', str(file), 'marker-fixture'])
                    result = subprocess.run(cmd, env=env, text=True, capture_output=True)
                    self.assertIn('Read-only file system', result.stderr)
                    self.assertNotEqual(result.returncode, 0, result.stderr)

    def test_claude_delegation_keeps_cli_and_hook_status_distinct(self):
        for hook in (False, True):
            with self.subTest(hook=hook):
                result = self.run_marker('spec', readonly=True, wrapper=True, hook=hook)
                self.assertIn('Read-only file system', result.stderr)
                if hook:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('spec-read-marker: marker-write-failed', result.stderr)
                else:
                    self.assertNotEqual(result.returncode, 0, result.stderr)

    def test_spec_marker_survives_agent_home_switch(self):
        release_a = Path(self.tmp.name) / 'release-a'
        release_b = Path(self.tmp.name) / 'release-b'
        self.marker_dir.mkdir(mode=0o700, parents=True)
        for home, session in ((release_a, 'release-a-sid'), (release_b, 'release-b-sid')):
            home.mkdir()
            env = dict(self.env, AGENT_HOME=str(home))
            cmd = self.readonly(
                [str(ROOT/'hooks/spec-read-marker.sh'), '--file', str(self.spec), '--session', session],
            )
            separator = cmd.index('--')
            cmd[separator:separator] = ['--bind', str(self.marker_dir), str(self.marker_dir)]
            result = subprocess.run(cmd, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((self.marker_dir / f'{session}__{self.repo.as_posix().replace("/", "_")}').is_file())
            self.assertFalse((home / '.spec-grounding').exists())
        self.assertEqual(self.marker_dir.stat().st_mode & 0o777, 0o700)

    def test_plugin_and_python_use_the_same_state_directory(self):
        plugin_data = Path(self.tmp.name) / 'plugin-data'
        env = dict(self.env, AGENT_HOME=str(ROOT), CLAUDE_PLUGIN_DATA=str(plugin_data))
        result = subprocess.run(
            [str(ROOT/'hooks/spec-read-marker.sh'), '--file', str(self.spec), '--session', 'plugin-sid'],
            env=env, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any(self.marker_dir.glob('plugin-sid__*')))
        self.assertFalse((plugin_data / '.spec-grounding').exists())
        code = (
            'import sys; sys.path.insert(0, sys.argv[1]); '
            'from harness_state_roots import spec_grounding_dir; '
            'print(spec_grounding_dir(None))'
        )
        resolved = subprocess.check_output(
            [sys.executable, '-c', code, str(ROOT/'utilities')], env=env, text=True,
        ).strip()
        self.assertEqual(Path(resolved), self.marker_dir)
        for env_values, expected in (
            ({'XDG_STATE_HOME': str(Path(self.tmp.name)/'xdg'), 'HOME': self.tmp.name},
             Path(self.tmp.name)/'xdg'/'hearting'/'spec-grounding'),
            ({'HOME': self.tmp.name}, Path(self.tmp.name)/'.local'/'state'/'hearting'/'spec-grounding'),
        ):
            isolated = dict(os.environ)
            isolated.pop('HARNESS_STATE_ROOT', None)
            isolated.pop('AGENT_HOME', None)
            isolated.pop('XDG_STATE_HOME', None)
            isolated.update(env_values)
            self.assertEqual(Path(subprocess.check_output(
                [sys.executable, '-c', code, str(ROOT/'utilities')], env=isolated, text=True
            ).strip()), expected)

    def test_plugin_spec_hooks_keep_harness_identity_and_shared_spec_lookup(self):
        plugin = ROOT/'adapters/claude/plugin-marketplace/plugins/hearting-claude'
        hooks = json.loads((plugin/'hooks/hooks.json').read_text(encoding='utf-8'))
        commands = [hook['command'] for event in hooks['hooks'].values()
                    for group in event for hook in group['hooks']]
        self.assertTrue(any('spec-read-marker.sh' in command for command in commands))
        self.assertTrue(any('spec-sync-nudge.sh' in command for command in commands))
        self.assertTrue(all('CLAUDE_PLUGIN_DATA' not in command for command in commands))
        self.assertEqual((plugin/'hooks/spec-read-marker.sh').read_bytes(),
                         (ROOT/'hooks/spec-read-marker.sh').read_bytes())

        shared = self.art/'shared/spec/ref_fixture/revisions/rrev_01'
        shared.mkdir(parents=True)
        (shared/'prd.md').write_text('# Shared spec\nLEGACY_TOKEN=old\n', encoding='utf-8')
        (self.art/'shared/spec/ref_fixture/reference.json').write_text(
            json.dumps({'latest_revision_id': 'rrev_01'}), encoding='utf-8')
        edited = self.repo/'source.py'
        edited.write_text('new_value = 1\n', encoding='utf-8')
        env = dict(self.env, AGENT_HOME=str(ROOT), CLAUDE_PLUGIN_DATA=str(Path(self.tmp.name)/'plugin-data'))
        result = subprocess.run(
            ['bash', str(plugin/'hooks/spec-sync-nudge.sh'), '--file', str(edited),
             '--old', 'LEGACY_TOKEN=old', '--new', 'new_value=1', '--cwd', str(self.repo), '--format', 'text'],
            env=env, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('LEGACY_TOKEN=old', result.stdout)
        self.assertIn('shared/spec/ref_fixture/revisions/rrev_01/prd.md', result.stdout)
        self.assertFalse(self.marker_dir.exists(), 'the sync nudge remains read-only')
        self.assertFalse((Path(self.tmp.name)/'plugin-data'/'utilities').exists())

    def test_g4_fixture_and_assert_use_same_state_root(self):
        work = Path(self.tmp.name) / 'g4-work'
        env = dict(self.env, AGENT_HOME=str(ROOT), DRILL_MARKER_DIR=str(self.marker_dir),
                   AGENT_ARTIFACT_ROOT=str(work/'repo/.claude_reports'))
        fixture = ROOT/'loops/drill/cases/g4_spec_gate/fixture.sh'
        result = subprocess.run(['bash', str(fixture), str(work)], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        prd = work/'repo/.claude_reports/spec/prd.md'
        result = subprocess.run(
            [str(ROOT/'hooks/spec-read-marker.sh'), '--file', str(prd), '--session', 'g4-fixture-sid'],
            env=env, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        transcript = Path(self.tmp.name) / 'g4-transcript.txt'
        transcript.write_text('spec-significant: option added\n', encoding='utf-8')
        assertion = ROOT/'loops/drill/cases/g4_spec_gate/assert.sh'
        result = subprocess.run(['bash', str(assertion), str(work), str(transcript)],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertTrue(any(self.marker_dir.glob('g4-fixture-sid__*')))
        self.assertFalse((self.home/'.spec-grounding').exists())
        self.assertFalse((work/'repo/.spec-grounding').exists())


if __name__ == '__main__':
    unittest.main()
