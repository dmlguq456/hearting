#!/usr/bin/env python3
"""실제 CLI와 임시 producer root를 쓰는 sealed-bytes restore 회귀. 운영 입력0."""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import artifact_restore_sealed as S

spec = importlib.util.spec_from_file_location('restore_producer_test_support', HERE / 'artifact_producer.test.py')
B = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = B
spec.loader.exec_module(B)
UTILITY = HERE / 'artifact_restore_sealed.py'


def sha_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RestoreTest(B.ProducerTestBase):
    def setUp(self):
        self.saved_env = dict(os.environ)
        self.addCleanup(self.restore_environment)
        for name in list(os.environ):
            if name.startswith('AGENT_') or name in ('CLAUDE_HOME', 'CODEX_THREAD_ID'):
                os.environ.pop(name, None)
        os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
        parent = os.environ.get('ARTIFACT_RESTORE_TEST_ROOT')
        if parent:
            p = Path(parent)
            self.assertTrue(p.is_dir(), 'NFS fixture parent를 먼저 준비해야 한다')
            mount = subprocess.run(['findmnt', '--json', '--target', str(p), '--output',
                                    'TARGET,FSTYPE,SOURCE,OPTIONS'], capture_output=True, text=True)
            self.assertEqual(mount.returncode, 0, mount.stderr)
            info = json.loads(mount.stdout)['filesystems'][0]
            self.assertIn(info['fstype'], ('nfs', 'nfs4'), 'nfs-required-unavailable')
            print('NFS_FIXTURE ' + json.dumps(info, sort_keys=True), flush=True)
            real = tempfile.TemporaryDirectory
            with mock.patch.object(B.tempfile, 'TemporaryDirectory',
                                   side_effect=lambda: real(prefix='sealed-restore-', dir=str(p))):
                super().setUp()
        else:
            super().setUp()
        self.addCleanup(self.assert_outside)
        self.outside = Path(self._tmp.name) / 'outside-sentinel'
        self.outside.write_bytes(b'outside:never touched')
        self.outside_before = self.file_identity(self.outside)
        (Path(self._tmp.name) / 'SYNTHETIC_RESTORE_FIXTURE').write_text('synthetic-only')
        self.route_number = 0
        self.activate()
        route, route_file, output = self.begin()
        self.output = output
        self.files = [
            self.write_output(output, 'plans/sample/bytes/a.bin', b'alpha\x00exact'),
            self.write_output(output, 'plans/sample/bytes/b.bin', b'beta\xffexact'),
            self.write_output(output, 'plans/sample/other/c.bin', b'gamma-exact'),
            self.write_output(output, 'plans/sample/third/d.bin', b'delta-exact'),
        ]
        self.content = {p: p.read_bytes() for p in self.files}
        self.close(route, route_file)
        B.P.finalize(self.root, cycle_id=output['cycle_id'])
        self.manifest_path = Path(output['cycle_dir']) / 'manifest.json'
        self.manifest = json.loads(self.manifest_path.read_text())
        self.backup = Path(self._tmp.name) / 'backup' / '20260101T000000Z'
        self.backup.mkdir(parents=True)
        self.archive = self.backup / 'retired-sources.tar.gz'
        self.members = [p.relative_to(Path(output['cycle_dir']) / 'artifacts').as_posix() for p in self.files]
        with tarfile.open(self.archive, 'w:gz') as tar:
            for p, member in zip(self.files, self.members):
                info = tarfile.TarInfo(member)
                info.mode = 0o640
                info.size = len(self.content[p])
                tar.addfile(info, io.BytesIO(self.content[p]))
        retired, journal = [], []
        for p, member in zip(self.files, self.members):
            row = dict(source=member, target=p.relative_to(self.root).as_posix(),
                       sha256=sha_file(p), size=len(self.content[p]))
            retired.append(row)
            journal.append(dict(schema_version='artifact-retirement-journal-row/v1',
                                row_ordinal=len(journal), action='retire_source', source_locator=member,
                                target_locator=row['target'], sha256=row['sha256'],
                                backup_archive=str(self.archive), commit_state='committed'))
        inventory = self.backup / 'retired-manifest.jsonl'
        inventory.write_bytes(b''.join(S.canonical(r) for r in retired))
        jp = self.root / '.runtime/artifact-producer/v1/migrations/20260101T000000Z-retirement/journal.jsonl'
        jp.parent.mkdir(parents=True)
        jp.write_bytes(b''.join(S.canonical(r) for r in journal))
        self.seal = self.backup / 'backup-seal.json'
        self.seal.write_bytes(S.canonical(dict(schema_version=1, archive=str(self.archive),
            archive_sha256=sha_file(self.archive), manifest_sha256=sha_file(inventory),
            file_count=4, byte_size=sum(len(v) for v in self.content.values()),
            artifact_root_id=B.ROOT_ID, created_at='2026-01-01T00:00:00Z')))
        self.immutable = {p: self.file_identity(p) for p in
                          (self.manifest_path, self.archive, self.seal, inventory, jp,
                           B.P.cycle_record_path(self.root, output['cycle_id']))}
        for p in self.files:
            # destructive-ok: reason=synthetic fixture models missing bytes; boundary=private TemporaryDirectory exact selected files
            p.unlink()
        for p in sorted({p.parent for p in self.files}):
            # destructive-ok: reason=synthetic fixture models missing ancestors; boundary=private TemporaryDirectory exact empty parents
            p.rmdir()
        _, _, self.work = self.begin()
        choices = []
        for p, member in zip(self.files, self.members):
            loc = p.relative_to(Path(output['cycle_dir'])).as_posix()
            revision = next(r for r in self.manifest['artifact_revisions'] if r['locator']['path'] == loc)
            choices.append(dict(cycle_id=output['cycle_id'], artifact_id=revision['artifact_id'],
                                artifact_revision_id=revision['artifact_revision_id'], member=member))
        self.selection = dict(schema_version=1, kind='sealed-bytes-selection', root=str(self.root),
            archive=dict(path=str(self.archive), sha256='sha256:' + sha_file(self.archive),
                         seal_path=str(self.seal), seal_sha256='sha256:' + sha_file(self.seal)),
            selections=choices)
        self.selection_path = Path(self._tmp.name) / 'selection.json'
        self.selection_path.write_bytes(S.canonical(self.selection))
        self.request_path = Path(self._tmp.name) / 'request.json'

    def restore_environment(self):
        os.environ.clear()
        os.environ.update(self.saved_env)

    def route(self, intensity='direct', capability='autopilot-code', mode='dev', **kw):
        self.route_number += 1
        route = B.compile_for(intensity, self.root, capability, mode, slug='restore-fixture-' + str(self.route_number))
        binding = B.L.admit_runtime_route(self.root, route)
        return route, Path(binding.route_file)

    @staticmethod
    def file_identity(p):
        st = p.stat()
        return sha_file(p), st.st_dev, st.st_ino, stat.S_IMODE(st.st_mode), st.st_mtime_ns

    def assert_outside(self):
        if hasattr(self, 'outside_before'):
            self.assertEqual(self.file_identity(self.outside), self.outside_before)

    def unchanged(self):
        for path, expected in self.immutable.items():
            self.assertEqual(self.file_identity(path), expected, str(path))

    def cli(self, *args, driver=None):
        cmd = [sys.executable, str(UTILITY)]
        if driver:
            cmd = [sys.executable, str(Path(__file__).resolve()), '--driver', driver,
                   '--fixture', self._tmp.name]
        return subprocess.run(cmd + list(args), cwd=self._tmp.name, capture_output=True,
                              timeout=50, env=dict(os.environ))

    def plan(self):
        result = self.cli('plan', '--request', str(self.selection_path))
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.request_path.write_bytes(result.stdout)
        self.request = json.loads(result.stdout)
        self.assertEqual(result.stderr.decode().strip(), S.digest(result.stdout))
        return result

    def apply(self, *, driver=None, dry=False):
        args = ['apply', '--request', str(self.request_path), '--expect-request-digest',
                S.digest(self.request_path.read_bytes()), '--work-cycle', self.work['cycle_id']]
        if dry:
            args.append('--dry-run')
        return self.cli(*args, driver=driver)

    def test_real_cli_four_files_dryrun_apply_verify_idempotent(self):
        self.plan()
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*'))
        r = self.apply(dry=True)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertEqual(json.loads(r.stdout)['status'], 'validated-dry-run')
        self.assertEqual(before, sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*')))
        r = self.apply()
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertEqual([x['disposition'] for x in json.loads(r.stdout)['files']], ['verified-created'] * 4)
        for p in self.files:
            self.assertEqual(p.read_bytes(), self.content[p])
        identities = {p: self.file_identity(p) for p in self.files}
        r = self.apply()
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertEqual(identities, {p: self.file_identity(p) for p in self.files})
        r = self.cli('verify', '--request', str(self.request_path))
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.unchanged()

    def test_directory_bundle_selection_is_typed_unsupported_before_mutation(self):
        selection = json.loads(self.selection_path.read_text())
        selected = selection["selections"][0]
        artifact = next(row for row in self.manifest["artifacts"]
                        if row["artifact_id"] == selected["artifact_id"])
        artifact["type"] = "directory-bundle"
        raw = S.canonical(self.manifest)
        self.manifest_path.write_bytes(raw)
        record = B.P.read_cycle_record(self.root, self.output["cycle_id"])
        record["manifest_digest"] = B.m.manifest_digest(self.manifest)
        B.P._write_cycle_record(self.root, record, exclusive=False)
        before = {path: path.exists() for path in self.files}
        result = self.cli("plan", "--request", str(self.selection_path))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"directory-bundle-restore-unsupported", result.stderr)
        for path, exists in before.items():
            self.assertEqual(path.exists(), exists)

    def test_last_target_foreign_preserves_all_before_mutation(self):
        self.plan()
        self.files[-1].parent.mkdir()
        self.files[-1].write_bytes(b'foreign')
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*'))
        r = self.apply()
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.files[0].exists())
        self.assertEqual(before, sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*')))
        self.assertEqual(self.files[-1].read_bytes(), b'foreign')
        self.unchanged()



    def test_selection_closed_json_and_raw_digest(self):
        original = self.selection_path.read_bytes()
        cases = [b'\xef\xbb\xbf' + original, b'{"kind":1,"kind":2}', b'{"n":NaN}',
                 S.canonical({**self.selection, 'authorized': True})]
        for bad in cases:
            with self.subTest(bad=bad[:40]):
                self.selection_path.write_bytes(bad)
                r = self.cli('plan', '--request', str(self.selection_path))
                self.assertEqual(r.returncode, 64, r.stderr.decode())
                self.assertFalse(any(p.exists() for p in self.files))
        self.selection_path.write_bytes(original)
        self.plan()
        args = ['apply', '--request', str(self.request_path), '--expect-request-digest',
                'sha256:' + '0' * 64, '--work-cycle', self.work['cycle_id']]
        self.assertEqual(self.cli(*args).returncode, 64)
        self.assertFalse(any(p.exists() for p in self.files))

    def test_request_unknown_noncanonical_and_wrong_ids(self):
        self.plan()
        original = self.request_path.read_bytes()
        cases = [original + b' ', S.canonical({**self.request, 'authorized': False})]
        for bad in cases:
            self.request_path.write_bytes(bad)
            r = self.apply()
            self.assertEqual(r.returncode, 64, r.stderr.decode())
        request = json.loads(original)
        request['files'][0]['artifact_id'] = 'art_' + '0' * 32
        self.request_path.write_bytes(S.canonical(request))
        self.assertNotEqual(self.apply().returncode, 0)
        self.assertFalse(any(p.exists() for p in self.files))
        self.unchanged()

    def test_duplicate_member_and_identity_rejected(self):
        for key in ('member', 'artifact_id', 'artifact_revision_id'):
            value = json.loads(S.canonical(self.selection))
            value['selections'][1][key] = value['selections'][0][key]
            self.selection_path.write_bytes(S.canonical(value))
            r = self.cli('plan', '--request', str(self.selection_path))
            self.assertNotEqual(r.returncode, 0)
        self.assertFalse(any(p.exists() for p in self.files))
        self.unchanged()

    def test_target_symlink_directory_and_foreign_hardlink(self):
        self.plan()
        p = self.files[-1]
        p.parent.mkdir()
        p.symlink_to(self.outside)
        self.assertNotEqual(self.apply().returncode, 0)
        self.assertTrue(p.is_symlink())
        # destructive-ok: reason=replace only fixture-created negative entry; boundary=private fixture last target
        p.unlink()
        p.mkdir()
        self.assertNotEqual(self.apply().returncode, 0)
        self.assertTrue(p.is_dir())
        # destructive-ok: reason=replace only empty fixture-created negative entry; boundary=private fixture last target
        p.rmdir()
        external = Path(self._tmp.name) / 'external-exact'
        external.write_bytes(self.content[p])
        os.link(external, p)
        self.assertNotEqual(self.apply().returncode, 0)
        self.assertEqual(p.stat().st_ino, external.stat().st_ino)
        self.assertFalse(self.files[0].exists())
        self.unchanged()

    def test_ancestor_symlink_preserved(self):
        self.plan()
        self.files[0].parent.symlink_to(Path(self._tmp.name), target_is_directory=True)
        r = self.apply()
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(self.files[0].parent.is_symlink())
        self.unchanged()

    def test_all_immutable_input_drift_and_same_bytes_inode(self):
        self.plan()
        for row in self.request['inputs']:
            path = Path(row['path'])
            original = path.read_bytes()
            path.write_bytes(original + b' ')
            r = self.apply()
            self.assertNotEqual(r.returncode, 0, row['role'])
            path.write_bytes(original)
        replacement = self.archive.with_suffix('.replacement')
        replacement.write_bytes(self.archive.read_bytes())
        # destructive-ok: reason=inject same-bytes inode drift in synthetic archive; boundary=private fixture archive only
        os.replace(replacement, self.archive)
        self.assertNotEqual(self.apply().returncode, 0)
        self.assertFalse(any(p.exists() for p in self.files))

    def test_sealed_work_cycle_and_selected_cycle_rejected(self):
        self.plan()
        original = self.work['cycle_id']
        self.work['cycle_id'] = self.output['cycle_id']
        r = self.apply()
        self.assertNotEqual(r.returncode, 0)
        self.work['cycle_id'] = original
        rp = B.P.cycle_record_path(self.root, original)
        record = json.loads(rp.read_text())
        record['state'] = 'sealed'
        rp.write_bytes(S.canonical(record))
        self.assertNotEqual(self.apply().returncode, 0)
        self.assertFalse(any(p.exists() for p in self.files))

    def test_retirement_target_other_cycle_rejected(self):
        inventory = self.backup / 'retired-manifest.jsonl'
        rows = [json.loads(x) for x in inventory.read_bytes().splitlines()]
        rows[0]['target'] = 'campaigns/foreign/cycles/foreign/artifacts/' + self.members[0]
        inventory.write_bytes(b''.join(S.canonical(r) for r in rows))
        seal = json.loads(self.seal.read_text())
        seal['manifest_sha256'] = sha_file(inventory)
        self.seal.write_bytes(S.canonical(seal))
        self.selection['archive']['seal_sha256'] = 'sha256:' + sha_file(self.seal)
        self.selection_path.write_bytes(S.canonical(self.selection))
        r = self.cli('plan', '--request', str(self.selection_path))
        self.assertNotEqual(r.returncode, 0)

    def test_work_route_drift_is_rejected_before_mutation(self):
        self.plan()
        record = B.P.read_cycle_record(self.root, self.work['cycle_id'])
        route_path = Path(record['route_file'])
        route = json.loads(route_path.read_text())
        route['slug'] = 'changed-without-valid-hash'
        route_path.write_bytes(S.canonical(route))
        self.assertNotEqual(self.apply(dry=True).returncode, 0)
        self.assertNotEqual(self.apply().returncode, 0)
        self.assertFalse(any(p.exists() for p in self.files))

    def test_record_io_failure_is_not_input_or_success(self):
        self.plan()
        r = self.apply(driver='record-io')
        self.assertEqual(r.returncode, 74, r.stderr.decode())
        self.assertEqual(json.loads(r.stderr)['reason'], 'verification-io')
        self.assertFalse(any(p.exists() for p in self.files))

    def test_tar_duplicate_link_sparse_and_wrong_bytes(self):
        for kind in ('duplicate', 'symlink', 'hardlink', 'sparse', 'bytes'):
            with tarfile.open(self.archive, 'w:gz') as tar:
                for index, (p, member) in enumerate(zip(self.files, self.members)):
                    info = tarfile.TarInfo(member)
                    info.mode, info.size = 0o640, len(self.content[p])
                    data = self.content[p]
                    if index == 0:
                        if kind in ('symlink', 'hardlink'):
                            info.type = tarfile.SYMTYPE if kind == 'symlink' else tarfile.LNKTYPE
                            info.linkname = '../outside-sentinel'
                        elif kind == 'sparse':
                            info.pax_headers['GNU.sparse.name'] = member
                        elif kind == 'bytes':
                            data = b'x' * len(data)
                    tar.addfile(info, io.BytesIO(data))
                    if index == 0 and kind == 'duplicate':
                        tar.addfile(info, io.BytesIO(data))
            seal = json.loads(self.seal.read_text())
            seal['archive_sha256'] = sha_file(self.archive)
            self.seal.write_bytes(S.canonical(seal))
            self.selection['archive'].update(sha256='sha256:' + sha_file(self.archive),
                                            seal_sha256='sha256:' + sha_file(self.seal))
            self.selection_path.write_bytes(S.canonical(self.selection))
            r = self.cli('plan', '--request', str(self.selection_path))
            self.assertNotEqual(r.returncode, 0, kind)
        self.assertFalse(any(p.exists() for p in self.files))

    def test_no_replace_race_preserves_foreign(self):
        self.plan()
        r = self.apply(driver='foreign-target')
        self.assertNotEqual(r.returncode, 0)
        first = self.root / self.request['files'][0]['target_locator']
        self.assertEqual(first.read_bytes(), b'foreign-race')
        self.assertFalse((self.root / self.request['files'][1]['target_locator']).exists())
        self.unchanged()

    def test_link_error_after_actual_publication_is_observed(self):
        self.plan()
        r = self.apply(driver='link-eio')
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertEqual([p.read_bytes() for p in self.files], [self.content[p] for p in self.files])
        self.unchanged()

    def test_observation_ambiguous_then_exact_resume(self):
        self.plan()
        r = self.apply(driver='ambiguous')
        self.assertEqual(r.returncode, 74, r.stderr.decode())
        first = self.root / self.request['files'][0]['target_locator']
        first_inode = first.stat().st_ino
        r = self.apply()
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertEqual(first.stat().st_ino, first_inode)
        self.assertEqual(first.stat().st_nlink, 1)
        self.unchanged()

    def test_unsupported_primitive_no_copy_fallback(self):
        self.plan()
        r = self.apply(driver='unsupported')
        self.assertEqual(r.returncode, 69, r.stderr.decode())
        self.assertFalse(any(p.exists() for p in self.files))
        self.unchanged()

    def test_exact_existing_race_preserves_unpublished_stage(self):
        self.plan()
        r = self.apply(driver='exact-existing-race')
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        result = json.loads(r.stdout)
        self.assertEqual(result['files'][0]['disposition'], 'verified-existing')
        self.assertEqual(len(result['inert_staging']), 1)
        stage = Path(result['inert_staging'][0])
        self.assertEqual(stage.read_bytes(), self.content[self.files[0]])
        self.assertNotEqual(stage.stat().st_ino, self.files[0].stat().st_ino)
        before = self.file_identity(self.files[0])
        r = self.apply()
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertEqual(self.file_identity(self.files[0]), before)
        self.assertTrue(stage.is_file())
        self.unchanged()

    def test_foreign_stage_and_corrupt_record_are_preserved(self):
        self.plan()
        r = self.apply(driver='crash-after-prepared')
        self.assertEqual(r.returncode, 86, r.stderr.decode())
        base = Path(self.work['cycle_dir']) / 'artifacts/plans/sealed-bytes-restore/_internal/requests' / S.digest(self.request_path.read_bytes())[7:]
        prepared = next((base / 'prepared').glob('*.json'))
        payload = json.loads(prepared.read_text())['payload']
        stage = Path(payload['stage_path'])
        stage.write_bytes(b'foreign-stage')
        self.assertNotEqual(self.apply().returncode, 0)
        self.assertEqual(stage.read_bytes(), b'foreign-stage')
        prepared.write_bytes(b'corrupt')
        self.assertNotEqual(self.apply().returncode, 0)
        self.assertEqual(prepared.read_bytes(), b'corrupt')
        self.assertFalse(any(p.exists() for p in self.files))

    def test_prepared_stage_bytes_mode_are_full_dryrun_validation(self):
        self.plan()
        self.assertEqual(self.apply(driver='crash-after-prepared').returncode, 86)
        base = Path(self.work['cycle_dir']) / 'artifacts/plans/sealed-bytes-restore/_internal/requests' / S.digest(self.request_path.read_bytes())[7:]
        payload = json.loads(next((base / 'prepared').glob('*.json')).read_text())['payload']
        stage = Path(payload['stage_path'])
        original = stage.read_bytes()
        stage.write_bytes(b'corrupt-stage')
        before = sorted(str(p) for p in self.root.rglob('*'))
        self.assertNotEqual(self.apply(dry=True).returncode, 0)
        self.assertNotEqual(self.apply().returncode, 0)
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob('*')))
        stage.write_bytes(original)
        stage.chmod(0o777)
        self.assertNotEqual(self.apply(dry=True).returncode, 0)
        self.assertNotEqual(self.apply().returncode, 0)
        self.assertFalse(any(p.exists() for p in self.files))
        self.unchanged()

    def test_lock_busy_is_bounded_without_target_writes(self):
        import fcntl
        self.plan()
        lock = self.root / '.runtime/artifact-admission/v1/lock.flock'
        with lock.open('a+b') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = lock.stat().st_ino
            r = self.apply()
            self.assertEqual(r.returncode, 75, r.stderr.decode())
            self.assertEqual(lock.stat().st_ino, before)
        self.assertFalse(any(p.exists() for p in self.files))
        self.unchanged()

    def test_ancestor_rename_is_detected_and_successor_preserved(self):
        self.plan()
        r = self.apply(driver='ancestor-rename')
        self.assertNotEqual(r.returncode, 0)
        parent = Path(self.output['cycle_dir']) / 'artifacts/plans/sample'
        self.assertTrue(parent.with_name('sample-moved').is_dir())
        self.assertFalse(parent.exists())
        self.unchanged()

    def test_source_drift_after_link_preserves_partial_and_stops(self):
        self.plan()
        r = self.apply(driver='source-drift-after-link')
        self.assertEqual(r.returncode, 74, r.stderr.decode())
        self.assertEqual(json.loads(r.stderr)['reason'], 'input-drift')
        self.assertEqual(sum(p.exists() for p in self.files), 1)
        first = self.root / self.request['files'][0]['target_locator']
        before = self.file_identity(first)
        self.assertNotEqual(self.apply().returncode, 0)
        self.assertEqual(before, self.file_identity(first))

    def test_request_bytes_swap_before_link_is_rejected(self):
        self.plan()
        r = self.apply(driver='request-swap')
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(any(p.exists() for p in self.files))
        self.unchanged()

    def test_foreign_result_record_never_becomes_success(self):
        self.plan()
        r = self.apply(driver='foreign-result')
        self.assertNotEqual(r.returncode, 0)
        base = Path(self.work['cycle_dir']) / 'artifacts/plans/sealed-bytes-restore/_internal/requests' / S.digest(self.request_path.read_bytes())[7:]
        self.assertEqual(next((base / 'results').glob('*.json')).read_bytes(), b'foreign-result')
        self.assertEqual(sum(p.exists() for p in self.files), 4)
        self.assertNotEqual(self.apply().returncode, 0)
        self.unchanged()

    def test_one_file_and_historical_id_layout(self):
        inventory = self.backup / 'retired-manifest.jsonl'
        journal = self.root / '.runtime/artifact-producer/v1/migrations/20260101T000000Z-retirement/journal.jsonl'
        rows = [json.loads(line) for line in inventory.read_bytes().splitlines()]
        events = [json.loads(line) for line in journal.read_bytes().splitlines()]
        for row, event in zip(rows, events):
            old = 'campaigns/' + self.output['campaign_id'] + '/cycles/' + self.output['cycle_id'] + '/artifacts/' + row['source']
            row['target'] = event['target_locator'] = old
        inventory.write_bytes(b''.join(S.canonical(r) for r in rows))
        journal.write_bytes(b''.join(S.canonical(r) for r in events))
        seal = json.loads(self.seal.read_text())
        seal['manifest_sha256'] = sha_file(inventory)
        self.seal.write_bytes(S.canonical(seal))
        self.selection['archive']['seal_sha256'] = 'sha256:' + sha_file(self.seal)
        self.selection['selections'] = self.selection['selections'][:1]
        self.selection_path.write_bytes(S.canonical(self.selection))
        self.plan()
        r = self.apply()
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertEqual(sum(p.exists() for p in self.files), 1)

    def crash_resume(self, phase):
        self.plan()
        r = self.apply(driver='crash-' + phase)
        self.assertEqual(r.returncode, 86, r.stderr.decode())
        present = {p: self.file_identity(p) for p in self.files if p.exists()}
        r = self.apply()
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        if phase == 'after-stage':
            self.assertEqual(len(json.loads(r.stdout)['inert_staging']), 1)
        for p, old in present.items():
            self.assertEqual(self.file_identity(p), old)
        self.assertEqual([p.read_bytes() for p in self.files], [self.content[p] for p in self.files])
        self.unchanged()

    def separate_request(self):
        self.request_path = Path(self._tmp.name) / 'request-only/request.json'
        self.request_path.parent.mkdir()
        self.plan()

    def dry_run_tree(self):
        result = {}
        for p in [self.root, *self.root.rglob('*'), self.request_path.parent, self.request_path]:
            st = p.lstat()
            data = p.read_bytes() if stat.S_ISREG(st.st_mode) else (
                os.readlink(p) if stat.S_ISLNK(st.st_mode) else None)
            result[str(p)] = (st.st_dev, st.st_ino, st.st_mode, st.st_mtime_ns, data)
        return result

    def separate_request_resume(self, driver):
        self.separate_request()
        r = self.apply(driver=driver)
        self.assertEqual(r.returncode, 86, r.stderr.decode())
        before = self.dry_run_tree()
        present = {p: self.file_identity(p) for p in self.files if p.exists()}
        r = self.apply(dry=True)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertEqual(json.loads(r.stdout)['writes'], 0)
        self.assertEqual(self.dry_run_tree(), before)
        r = self.apply()
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        for p, expected in present.items():
            self.assertEqual(self.file_identity(p), expected)
        before = self.dry_run_tree()
        r = self.apply(dry=True)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertEqual(json.loads(r.stdout)['writes'], 0)
        self.assertEqual(self.dry_run_tree(), before)
        self.unchanged()

    def test_request_drift_between_dry_run_snapshots(self):
        self.separate_request()
        r = self.apply(dry=True, driver='second-request-bytes')
        self.assertEqual(r.returncode, 65, r.stderr.decode())
        self.assertIn('input-drift', r.stderr.decode())
        self.assertFalse(any(p.exists() for p in self.files))
        self.unchanged()

    def test_request_ancestor_replacement_between_dry_run_snapshots(self):
        self.separate_request()
        r = self.apply(dry=True, driver='second-request-ancestor')
        self.assertEqual(r.returncode, 65, r.stderr.decode())
        self.assertFalse(any(p.exists() for p in self.files))
        self.unchanged()



def driver(argv):
    name = argv[0]
    assert argv[1] == '--fixture'
    fixture = Path(argv[2]).resolve()
    assert (fixture / 'SYNTHETIC_RESTORE_FIXTURE').read_text() == 'synthetic-only'
    args = argv[3:]
    request = Path(args[args.index('--request') + 1]).resolve()
    request.relative_to(fixture)
    document = json.loads(request.read_text())
    Path(document['root']['path']).resolve().relative_to(fixture)
    Path(document['archive']['path']).resolve().relative_to(fixture)
    original_link = S._publish_link
    original_snapshot_init = S.Snapshot.__init__
    snapshot_count = 0
    def snapshot_init(snapshot):
        nonlocal snapshot_count
        original_snapshot_init(snapshot)
        snapshot_count += 1
        if snapshot_count == 2 and name == 'second-request-bytes':
            request.write_bytes(request.read_bytes() + b' ')
        if snapshot_count == 2 and name == 'second-request-ancestor':
            parent = request.parent
            prior = request.read_bytes()
            # destructive-ok: reason=inject ancestor replacement in private synthetic request; boundary=fixture request-only directory and its sibling
            parent.rename(parent.with_name('request-only-moved'))
            parent.mkdir()
            request.write_bytes(prior)
    S.Snapshot.__init__ = snapshot_init
    def link(a, b, c, d):
        if name == 'unsupported':
            raise OSError(errno.EXDEV, 'synthetic unsupported filesystem')
        original_link(a, b, c, d)
        if name == 'link-eio':
            raise OSError(errno.EIO, 'synthetic response loss after actual link')
    def checkpoint(phase, context):
        if phase == 'record-published' and name == 'record-crash-' + context['record_kind']:
            os._exit(86)
        if phase == 'record-published' and name == 'record-io':
            raise OSError(errno.EIO, 'synthetic record publication IO failure')
        if name == 'ancestor-rename' and phase == 'before-link' and context['index'] == 0:
            target = Path(context['request']['root']['path']) / context['request']['files'][0]['target_locator']
            parent = target.parent.parent
            # destructive-ok: reason=inject rename race in private synthetic input; boundary=fixture sample ancestor and its sibling
            parent.rename(parent.with_name('sample-moved'))
        if name == 'source-drift-after-link' and phase == 'after-link' and context['index'] == 0:
            manifest = Path(context['request']['cycles'][0]['manifest_path'])
            manifest.write_bytes(manifest.read_bytes() + b' ')
        if name == 'request-swap' and phase == 'before-link' and context['index'] == 0:
            request.write_bytes(request.read_bytes() + b' ')
        if name == 'foreign-result' and phase == 'before-result':
            records = context['records']
            (Path(records.base) / 'results' / (str(records.sequence) + '.json')).write_bytes(b'foreign-result')
        if name == 'foreign-target' and phase == 'before-link' and context['index'] == 0:
            file = context['request']['files'][0]
            path = Path(context['request']['root']['path']) / file['target_locator']
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'foreign-race')
        if name == 'exact-existing-race' and phase == 'before-link' and context['index'] == 0:
            file = context['request']['files'][0]
            path = Path(context['request']['root']['path']) / file['target_locator']
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(Path(context['records'].prepared[0]['stage_path']).read_bytes())
        if name == 'ambiguous' and phase == 'after-link' and context['index'] == 0:
            original_open = S.Tree.open_file
            target = str(Path(context['request']['root']['path']) / context['request']['files'][0]['target_locator'])
            def unobservable(tree, path, *a, **kw):
                if path == target:
                    raise OSError(errno.EIO, 'synthetic target observation unavailable')
                return original_open(tree, path, *a, **kw)
            S.Tree.open_file = unobservable
        if name == 'crash-' + phase and (context.get('index') in (None, 0) or
                                       phase in ('before-result', 'after-result')):
            os._exit(86)
    S._publish_link, S._checkpoint = link, checkpoint
    return S.main(args)


for _phase in ('validated', 'before-stage', 'after-stage', 'after-prepared', 'before-link',
               'after-link', 'before-cleanup', 'after-cleanup', 'before-result', 'after-result'):
    def _test(self, phase=_phase):
        self.crash_resume(phase)
    setattr(RestoreTest, 'test_crash_resume_' + _phase.replace('-', '_'), _test)

for _kind in ('request', 'prepared', 'observations', 'results'):
    def _record_test(self, kind=_kind):
        self.plan()
        r = self.apply(driver='record-crash-' + kind)
        self.assertEqual(r.returncode, 86, r.stderr.decode())
        before = {p: self.file_identity(p) for p in self.files if p.exists()}
        self.assertEqual(self.apply(dry=True).returncode, 0)
        r = self.apply()
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        for p, value in before.items():
            self.assertEqual(self.file_identity(p), value)
        self.unchanged()
    setattr(RestoreTest, 'test_record_publication_crash_' + _kind, _record_test)

for _driver in ('crash-after-prepared', 'crash-after-link', 'record-crash-request',
                'record-crash-prepared', 'record-crash-observations', 'record-crash-results'):
    def _separate_test(self, driver=_driver):
        self.separate_request_resume(driver)
    setattr(RestoreTest, 'test_separate_request_' + _driver.replace('-', '_'), _separate_test)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--driver':
        raise SystemExit(driver(sys.argv[2:]))
    unittest.main()
