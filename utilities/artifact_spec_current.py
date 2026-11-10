"""Derived latest-spec view. Immutable revisions remain the source of truth.

Only normal publication calls refresh. Reads validate the pointer, marker and
requested bytes, and return None on drift; they never repair the view.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat

from artifact_nofollow import open_relative, open_root
from artifact_spec_storage import StorageError, _digest, _parts, read_files, read_inventory

SCHEMA = 'shared-spec-current/v1'


def _reference_fd(root, reference):
    fd = open_root(Path(root).resolve())
    try:
        return open_relative(fd, ['shared', 'spec', reference])
    finally:
        os.close(fd)


def _bytes(fd, parts, bound):
    child = open_relative(fd, parts)
    with os.fdopen(child, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('view entry is not regular')
        raw = stream.read(bound + 1)
    if len(raw) > bound:
        raise ValueError('view entry exceeds its expected size')
    return raw


def _json(fd, parts):
    result = json.loads(_bytes(fd, parts, 32 * 1024 * 1024))
    if not isinstance(result, dict):
        raise ValueError('view metadata is not an object')
    return result


def _parent(fd, parts, *, create=False):
    parent = os.dup(fd)
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, dir_fd=parent)
                except FileExistsError:
                    pass
            child = open_relative(parent, [part])
            if not stat.S_ISDIR(os.fstat(child).st_mode):
                os.close(child)
                raise ValueError('view parent is not a directory')
            os.close(parent); parent = child
        return parent
    except BaseException:
        os.close(parent)
        raise


def _put(fd, parts, raw):
    parent = _parent(fd, parts, create=True)
    temporary = '.current-' + os.urandom(12).hex()
    try:
        try:
            mode = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False).st_mode
            if not stat.S_ISREG(mode):
                raise ValueError('view target is not a regular file')
        except FileNotFoundError:
            pass
        child = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=parent)
        with os.fdopen(child, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


def _names(fd, prefix=''):
    names = set()
    with os.scandir(fd) as entries:
        for entry in entries:
            name = prefix + entry.name
            mode = entry.stat(follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode):
                child = open_relative(fd, [entry.name])
                try:
                    names.update(_names(child, name + '/'))
                finally:
                    os.close(child)
            elif stat.S_ISREG(mode):
                # A killed atomic replacement can leave a private temporary
                # file. Ignore it without adopting or deleting its bytes.
                if not re.fullmatch(r'\.current-[0-9a-f]{24}', entry.name):
                    names.add(name)
            else:
                raise ValueError('linked or special view entry')
    return names


def refresh(root, reference, revision):
    """Best-effort projection after the existing immutable pointer commit.

    Interrupted projection is harmless to admission and immutable reads. An
    old journal replay never rewinds a view belonging to a newer pointer.
    """
    ref_fd = view_fd = None
    try:
        files, record = read_files(root, reference, revision)
        ref_fd = _reference_fd(root, reference)
        if _json(ref_fd, ['reference.json']).get('latest_revision_id') != revision:
            return {'state': 'skipped', 'reason': 'newer-latest'}
        try:
            os.mkdir('current', dir_fd=ref_fd)
        except FileExistsError:
            pass
        view_fd = open_relative(ref_fd, ['current'])
        names = _names(view_fd) - {'revision.json'}
        try:
            marker = _json(view_fd, ['revision.json'])
        except FileNotFoundError:
            marker = None
        old_names = set()
        if marker is not None:
            if marker.get('schema') != SCHEMA or marker.get('reference_id') != reference:
                raise ValueError('foreign current view')
            old = read_inventory(root, reference, marker.get('revision_id'))
            if marker.get('content_digest') != old['content_digest']:
                raise ValueError('unbound current view')
            old_names = {row['path'] for row in old['files']}
        if names - old_names - set(files):
            raise ValueError('unknown current view files')
        # An unmarked interrupted first projection can be resumed only for
        # exact matching bytes; it cannot claim arbitrary existing content.
        if marker is None:
            for name in names:
                if _bytes(view_fd, _parts(name), files[name].byte_size) != files[name].data:
                    raise ValueError('unowned current view bytes')
        written = written_bytes = deleted = 0
        for name, file in files.items():
            try:
                unchanged = _bytes(view_fd, _parts(name), file.byte_size) == file.data
            except (OSError, ValueError):
                unchanged = False
            if not unchanged:
                _put(view_fd, _parts(name), file.data)
                written += 1; written_bytes += file.byte_size
        for name in sorted(names - set(files)):
            parts = _parts(name); parent = _parent(view_fd, parts)
            try:
                os.unlink(parts[-1], dir_fd=parent); os.fsync(parent); deleted += 1
            finally:
                os.close(parent)
        if _json(ref_fd, ['reference.json']).get('latest_revision_id') != revision:
            return {'state': 'unknown', 'reason': 'latest-moved'}
        marker = {'schema': SCHEMA, 'reference_id': reference, 'revision_id': revision,
            'content_digest': record['content_digest']}
        _put(view_fd, ['revision.json'], (json.dumps(marker, sort_keys=True) + '\n').encode())
        return {'state': 'updated', 'file_count': written, 'byte_size': written_bytes, 'deleted_files': deleted}
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return {'state': 'unknown', 'reason': 'view-unavailable'}
    finally:
        if view_fd is not None: os.close(view_fd)
        if ref_fd is not None: os.close(ref_fd)


@dataclass(frozen=True)
class CurrentFile:
    data: bytes
    path: Path
    reference_id: str
    revision_id: str
    logical_path: str
    sha256: str
    byte_size: int


def read_file(root, reference, revision, logical):
    """A verified latest-view file or None; callers use immutable storage next."""
    ref_fd = view_fd = None
    try:
        parts = _parts(logical)
        record = read_inventory(root, reference, revision)
        expected = next(row for row in record['files'] if row['path'] == logical)
        ref_fd = _reference_fd(root, reference)
        before = _bytes(ref_fd, ['reference.json'], 32 * 1024 * 1024)
        if json.loads(before).get('latest_revision_id') != revision:
            return None
        view_fd = open_relative(ref_fd, ['current'])
        marker = _bytes(view_fd, ['revision.json'], 32 * 1024 * 1024)
        if json.loads(marker) != {'schema': SCHEMA, 'reference_id': reference,
                'revision_id': revision, 'content_digest': record['content_digest']}:
            return None
        raw = _bytes(view_fd, parts, expected['byte_size'])
        if len(raw) != expected['byte_size'] or _digest(raw) != expected['sha256']:
            return None
        if (_bytes(view_fd, ['revision.json'], 32 * 1024 * 1024) != marker or
                _bytes(ref_fd, ['reference.json'], 32 * 1024 * 1024) != before):
            return None
        return CurrentFile(raw, Path(root).resolve() / 'shared/spec' / reference / 'current' / logical,
            reference, revision, logical, expected['sha256'], expected['byte_size'])
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, StopIteration):
        return None
    finally:
        if view_fd is not None: os.close(view_fd)
        if ref_fd is not None: os.close(ref_fd)


def address(root, relative):
    """Existing latest identity for a current file/directory address, or None."""
    parts = relative.split('/')
    if len(parts) < 4 or parts[:2] != ['shared', 'spec'] or parts[3] != 'current':
        return None
    reference, logical = parts[2], '/'.join(parts[4:])
    from artifact_identity import is_well_formed
    if not is_well_formed(reference, 'shared_reference'):
        return None
    fd = None
    try:
        if logical: _parts(logical)
        fd = _reference_fd(root, reference)
        record = _json(fd, ['reference.json'])
        if (record.get('shared_reference_id') != reference or
                not is_well_formed(record.get('latest_revision_id'), 'shared_reference_revision')):
            raise ValueError('reference identity mismatch')
        return reference, record['latest_revision_id'], logical
    finally:
        if fd is not None: os.close(fd)


def resolve_path(root, relative):
    """Resolve the existing stable current address, including immutable fallback."""
    from artifact_spec_storage import resolve_path as immutable_resolve
    try:
        found = address(root, relative)
        if found is None or not found[2]: return None
        reference, revision, logical = found
        current = read_file(root, reference, revision, logical)
        if current is not None:
            return {'path': relative, 'resolution': 'current', 'target': relative,
                'absolute': str(current.path), 'reference_id': reference,
                'revision_id': revision, 'logical_path': logical,
                'sha256': current.sha256, 'byte_size': current.byte_size, 'view': True}
        result = immutable_resolve(root, f'shared/spec/{reference}/revisions/{revision}/{logical}')
        if result is None:
            raise ValueError('logical revision is unavailable')
        return {**result, 'path': relative, 'view': False}
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return {'path': relative, 'resolution': 'unknown', 'absolute': None, 'target': None}
