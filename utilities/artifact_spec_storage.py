"""Logical shared-spec files over full-tree and direct-reference revisions.

No writes, reference-chain traversal, persistent cache or producer imports.
One read snapshots metadata and verifies the bytes it returns. A file lookup
verifies that file; read_revision verifies the entire logical inventory.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat

import artifact_identity
from artifact_nofollow import open_relative, open_root

META_LIMIT = 32 * 1024 * 1024
_SHA = re.compile(r'^sha256:[0-9a-f]{64}$')


class StorageError(ValueError):
    def __init__(self, detail, *, code='shared-revision-integrity'):
        super().__init__(detail)
        self.code = code


def _digest(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def _inventory_digest(rows):
    value = [[row['path'], row['sha256'], row['byte_size']] for row in rows]
    return _digest(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())


def _parts(value):
    if (not isinstance(value, str) or not value or value.startswith('/')
            or '\\' in value or '\x00' in value
            or any(part in ('', '.', '..') for part in value.split('/'))
            or value == 'revision.json'):
        raise StorageError('unsafe logical or source path')
    return value.split('/')


def _revision_id(value):
    if not artifact_identity.is_well_formed(value, 'shared_reference_revision'):
        raise StorageError('invalid storage revision')
    return value


@dataclass(frozen=True)
class SpecFile:
    data: bytes
    path: Path
    logical_path: str
    reference_id: str
    revision_id: str
    source_revision_id: str
    source_path: str
    sha256: str
    byte_size: int


class _Snapshot:
    def __init__(self, root, reference_id, revision_id):
        if not artifact_identity.is_well_formed(reference_id, 'shared_reference'):
            raise StorageError('invalid reference', code='shared-base-invalid')
        if not artifact_identity.is_well_formed(revision_id, 'shared_reference_revision'):
            raise StorageError('invalid revision', code='shared-base-invalid')
        self.root = Path(root).resolve()
        self.reference_id, self.revision_id = reference_id, revision_id
        self.fd = None
        self.headers, self.data = {}, {}
        self.reference = None
        root_fd = None
        try:
            root_fd = open_root(self.root)
            self.fd = open_relative(root_fd, ['shared', 'spec', reference_id])
            self.header(revision_id, initial=True)
        except StorageError:
            self.close()
            raise
        except (OSError, RuntimeError, ValueError) as error:
            self.close()
            raise StorageError(str(error), code='shared-base-invalid') from error
        finally:
            if root_fd is not None:
                os.close(root_fd)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _json(self, parts):
        fd = open_relative(self.fd, parts)
        with os.fdopen(fd, 'rb') as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise StorageError('metadata is not regular')
            raw = handle.read(META_LIMIT + 1)
        if len(raw) > META_LIMIT:
            raise StorageError('metadata exceeds read bound')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise StorageError('metadata is not an object')
        return value

    def header(self, revision, *, initial=False):
        if revision in self.headers:
            return self.headers[revision]
        try:
            record = self._json(['revisions', revision, 'revision.json'])
        except (OSError, RuntimeError, ValueError) as error:
            raise StorageError(str(error), code='shared-base-invalid' if initial else 'shared-revision-integrity') from error
        if (record.get('shared_reference_id') != self.reference_id
                or record.get('shared_reference_revision_id') != revision):
            raise StorageError('revision identity mismatch',
                code='shared-base-invalid' if initial else 'shared-revision-integrity')
        version = record.get('schema_version', 1)
        if type(version) is not int or version not in (1, 2):
            raise StorageError('unsupported revision storage schema')
        rows = record.get('files')
        if not isinstance(rows, list):
            raise StorageError('logical inventory missing')
        entries = {}
        for row in rows:
            if not isinstance(row, dict) or set(row) != {'path', 'sha256', 'byte_size'}:
                raise StorageError('invalid inventory row')
            name, size, digest = row['path'], row['byte_size'], row['sha256']
            _parts(name)
            if (name in entries or not isinstance(size, int) or isinstance(size, bool) or size < 0
                    or not isinstance(digest, str) or not _SHA.fullmatch(digest)):
                raise StorageError('invalid inventory facts')
            entries[name] = row
        if record.get('content_digest') != _inventory_digest(rows):
            raise StorageError('logical inventory digest mismatch')
        for key, actual in (('file_count', len(rows)), ('byte_size', sum(row['byte_size'] for row in rows))):
            if key in record and (type(record[key]) is not int or record[key] != actual):
                raise StorageError('logical inventory size mismatch')
        stored = record.get('stored_in', {})
        if not isinstance(stored, dict) or any(name not in entries for name in stored):
            raise StorageError('invalid storage inventory')
        sources = {}
        for name, value in stored.items():
            if version == 1:
                provider, source_path = _revision_id(value), name
            elif isinstance(value, dict) and set(value) == {'revision_id', 'source_path'}:
                provider, source_path = _revision_id(value['revision_id']), value['source_path']
                _parts(source_path)
            else:
                raise StorageError('invalid direct storage source')
            sources[name] = (provider, source_path)
        result = record, entries, sources
        self.headers[revision] = result
        return result

    def roster(self):
        record, entries, sources = self.header(self.revision_id)
        fd = open_relative(self.fd, ['revisions', self.revision_id])
        found = set()
        def walk(parent, prefix=''):
            with os.scandir(parent) as items:
                for entry in items:
                    rel = prefix + entry.name
                    mode = entry.stat(follow_symlinks=False).st_mode
                    if stat.S_ISDIR(mode):
                        child = open_relative(parent, [entry.name])
                        try:
                            walk(child, rel + '/')
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(mode):
                        if rel != 'revision.json':
                            found.add(rel)
                    else:
                        raise StorageError('linked or special revision entry')
        try:
            walk(fd)
        finally:
            os.close(fd)
        if found != set(entries) - set(sources):
            raise StorageError('physical and logical storage declarations disagree')

    def _earlier(self, provider):
        if provider == self.revision_id:
            raise StorageError('storage source is the same revision')
        if self.reference is None:
            self.reference = self._json(['reference.json'])
        reference = self.reference
        revisions = reference.get('revisions')
        if (reference.get('shared_reference_id') != self.reference_id or not isinstance(revisions, list)
                or any(not artifact_identity.is_well_formed(value, 'shared_reference_revision') for value in revisions)
                or len(set(revisions)) != len(revisions) or provider not in revisions):
            raise StorageError('storage source is not in this reference')
        if self.revision_id in revisions:
            if revisions.index(provider) >= revisions.index(self.revision_id):
                raise StorageError('storage source is not earlier')
        else:
            # Normal publication verifies its immutable target before the
            # existing CAS appends that target to the reference roster.
            sequence = self.header(self.revision_id)[0].get('sequence')
            if type(sequence) is not int or sequence != len(revisions) + 1:
                raise StorageError('uncommitted storage revision has no next sequence')

    def file(self, logical):
        _parts(logical)
        _, entries, sources = self.header(self.revision_id)
        if logical not in entries:
            raise StorageError('logical file absent')
        expected = entries[logical]
        provider, source_path = sources.get(logical, (self.revision_id, logical))
        if provider != self.revision_id:
            self._earlier(provider)
            _, prior_entries, prior_sources = self.header(provider)
            prior = prior_entries.get(source_path)
            if (prior is None or source_path in prior_sources
                    or (prior['sha256'], prior['byte_size']) != (expected['sha256'], expected['byte_size'])):
                raise StorageError('storage source is not the matching physical file')
        key = provider, source_path
        if key not in self.data:
            fd = open_relative(self.fd, ['revisions', provider, *_parts(source_path)])
            with os.fdopen(fd, 'rb') as handle:
                before = os.fstat(handle.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size != expected['byte_size']:
                    raise StorageError('physical source size/type mismatch')
                raw = handle.read(expected['byte_size'] + 1)
                after = os.fstat(handle.fileno())
            digest = _digest(raw)
            if ((before.st_mtime_ns, before.st_ctime_ns, before.st_size) !=
                    (after.st_mtime_ns, after.st_ctime_ns, after.st_size)
                    or len(raw) != expected['byte_size'] or digest != expected['sha256']):
                raise StorageError('physical source bytes mismatch')
            self.data[key] = raw, digest
        raw, digest = self.data[key]
        if len(raw) != expected['byte_size'] or digest != expected['sha256']:
            raise StorageError('one source has conflicting logical declarations')
        path = self.root / 'shared/spec' / self.reference_id / 'revisions' / provider / source_path
        return SpecFile(raw, path, logical, self.reference_id, self.revision_id,
                        provider, source_path, expected['sha256'], expected['byte_size'])


def read_revision(root, reference_id, revision_id):
    """The complete verified logical tree and its unchanged metadata."""
    snapshot = None
    try:
        snapshot = _Snapshot(root, reference_id, revision_id)
        snapshot.roster()
        record, entries, _ = snapshot.header(revision_id)
        return {name: snapshot.file(name).data for name in sorted(entries)}, record
    except StorageError:
        raise
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as error:
        raise StorageError(str(error)) from error
    finally:
        if snapshot is not None:
            snapshot.close()


def read_file(root, reference_id, revision_id, logical_path, *, missing_ok=False):
    """Verify a single logical file, without claiming unrelated file health."""
    snapshot = None
    try:
        snapshot = _Snapshot(root, reference_id, revision_id)
        snapshot.roster()
        if missing_ok and logical_path not in snapshot.header(revision_id)[1]:
            _parts(logical_path)
            return None
        return snapshot.file(logical_path)
    except StorageError:
        raise
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as error:
        raise StorageError(str(error)) from error
    finally:
        if snapshot is not None:
            snapshot.close()


def read_files(root, reference_id, revision_id, *, paths=None):
    """Verified files and physical addresses in one snapshot.

    With paths, only the selected payload bytes are verified; the complete
    metadata and physical roster are still checked. No persistent cache.
    """
    snapshot = None
    try:
        snapshot = _Snapshot(root, reference_id, revision_id)
        snapshot.roster()
        record, entries, _ = snapshot.header(revision_id)
        selected = entries if paths is None else paths
        return {name: snapshot.file(name) for name in sorted(selected)}, record
    except StorageError:
        raise
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as error:
        raise StorageError(str(error)) from error
    finally:
        if snapshot is not None:
            snapshot.close()


def read_inventory(root, reference_id, revision_id):
    """Verified metadata/physical roster; not a claim about payload byte health."""
    snapshot = None
    try:
        snapshot = _Snapshot(root, reference_id, revision_id)
        snapshot.roster()
        return snapshot.header(revision_id)[0]
    except StorageError:
        raise
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as error:
        raise StorageError(str(error)) from error
    finally:
        if snapshot is not None:
            snapshot.close()


def revision_address(root, path):
    """Split an existing immutable address; never turn a path into a new ID.

    Unrecorded pre-cutover directories retain their legacy reader behavior.
    A present, unreadable or malformed revision record is not legacy absence.
    """
    try:
        relative = Path(path).absolute().relative_to(Path(root).absolute())
        parts = relative.parts
        if (len(parts) >= 5 and parts[:2] == ('shared', 'spec') and parts[3] == 'revisions'
                and artifact_identity.is_well_formed(parts[2], 'shared_reference')
                and artifact_identity.is_well_formed(parts[4], 'shared_reference_revision')
                and os.path.lexists(Path(root).joinpath(*parts[:5], 'revision.json'))):
            return parts[2], parts[4], '/'.join(parts[5:])
    except (OSError, ValueError):
        pass
    return None


def resolve_path(root, relative):
    """Existing resolve response, extended with logical and physical identities."""
    from artifact_spec_current import resolve_path as current_resolve
    current = current_resolve(root, relative)
    if current is not None:
        return current
    address = revision_address(root, Path(root) / relative)
    if address is None or not address[2]:
        return None
    reference, revision, logical = address
    try:
        file = read_file(root, reference, revision, logical)
        target = file.path.relative_to(Path(root).resolve()).as_posix()
        return {'path': relative, 'resolution': 'present' if (revision, logical) ==
            (file.source_revision_id, file.source_path) else 'stored',
            'target': target, 'absolute': str(file.path), 'reference_id': reference,
            'revision_id': revision, 'logical_path': logical,
            'source_revision_id': file.source_revision_id, 'source_path': file.source_path,
            'sha256': file.sha256, 'byte_size': file.byte_size}
    except StorageError as error:
        return {'path': relative, 'resolution': 'unknown', 'target': None, 'absolute': None,
            'reference_id': reference, 'revision_id': revision, 'logical_path': logical,
            'error': error.code}


def entrance_snapshot(root, *, limit=300):
    """Canonical topics from existing reference/component/revision identities."""
    root = Path(root).resolve()
    rows, errors = [], []
    root_fd = shared_fd = None
    coverage = 'complete'
    try:
        root_fd = open_root(root)
        try:
            shared_fd = open_relative(root_fd, ['shared', 'spec'])
        except FileNotFoundError:
            return {'kind': 'canonical-topic', 'coverage': 'complete', 'rows': [], 'errors': []}
        owner = None
        try:
            fd = open_relative(root_fd, ['.runtime', 'artifact-admission', 'v1', 'root-identity.json'])
            with os.fdopen(fd, 'rb') as stream:
                raw = stream.read(META_LIMIT + 1)
            if len(raw) <= META_LIMIT:
                owner = artifact_identity.RootIdentity.parse(json.loads(raw))
        except (OSError, ValueError, TypeError, KeyError, artifact_identity.IdentityError):
            pass
        # Bound the source independently of how many cycles an entrance found.
        references = []
        with os.scandir(shared_fd) as entries:
            for entry in entries:
                if len(references) >= limit:
                    coverage = 'partial'; break
                if entry.name.startswith('.'):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    references.append(entry.name)
                else:
                    errors.append('reference-not-directory')
        for reference in sorted(references):
            try:
                fd = open_relative(shared_fd, [reference, 'reference.json'])
                with os.fdopen(fd, 'rb') as stream:
                    raw = stream.read(META_LIMIT + 1)
                if len(raw) > META_LIMIT:
                    raise StorageError('reference metadata exceeds read bound')
                record = json.loads(raw)
                if record.get('shared_reference_id') != reference:
                    raise StorageError('reference identity mismatch')
                revision = record.get('latest_revision_id')
                inventory = read_inventory(root, reference, revision)
                paths = [row['path'] for row in inventory['files'] if row['path'] == 'prd.md'
                         or (row['path'].count('/') == 1 and row['path'].endswith('/prd.md')
                             and not row['path'].startswith('_internal/'))]
                if len(rows) + len(paths) > limit:
                    paths = paths[:limit-len(rows)]; coverage = 'partial'
                files, _ = read_files(root, reference, revision, paths=paths)
                for logical, file in files.items():
                    component = logical.split('/')[0] if '/' in logical else None
                    date = str(inventory.get('created_on') or '')[:10]
                    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date): date = ''
                    rows.append({'kind': 'canonical-topic', 'title': component or record.get('title') or record.get('key') or reference,
                        'date': date, 'summary': '', 'doc_path': file.path.relative_to(root).as_posix(),
                        'artifact_root_id': owner.artifact_root_id if owner else None,
                        'repository_id': owner.repository_id if owner else None,
                        'campaign_id': None, 'cycle_id': None, 'cycle_path': None,
                        'artifact_id': None, 'artifact_revision_id': None,
                        'reference_id': reference, 'component': component, 'revision_id': revision,
                        'source_revision_id': file.source_revision_id, 'source_path': file.source_path,
                        'logical_locator': f'shared/spec/{reference}/revisions/{revision}/{logical}',
                        'status': 'recorded', 'coverage': 'complete', 'digest': file.sha256})
            except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError):
                errors.append('reference-unreadable')
        if errors and coverage == 'complete': coverage = 'unknown'
        return {'kind': 'canonical-topic', 'coverage': coverage, 'rows': rows, 'errors': errors}
    except (OSError, RuntimeError, ValueError):
        return {'kind': 'canonical-topic', 'coverage': 'unknown', 'rows': rows, 'errors': ['source-unreadable']}
    finally:
        if shared_fd is not None: os.close(shared_fd)
        if root_fd is not None: os.close(root_fd)
