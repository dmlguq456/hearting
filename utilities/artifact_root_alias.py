"""Best-effort legacy-root alias, called only after a normal producer write."""
import json
import os
from pathlib import Path

from artifact_nofollow import open_relative, open_root


def ensure_legacy_alias(root, identity=None):
    root = Path(root)
    if root.name != '.claude_reports':
        return None
    parent = legacy = None
    try:
        parent = open_root(root.parent)
        legacy = open_relative(parent, [root.name])
        observed = os.fstat(legacy)
        if identity is not None:
            fd = open_relative(legacy, ['.runtime', 'artifact-admission', 'v1', 'root-identity.json'])
            try:
                record = json.loads(os.read(fd, 65536))
            finally:
                os.close(fd)
            if (record.get('artifact_root_id'), record.get('repository_id')) != (
                    identity.artifact_root_id, identity.repository_id):
                return {'status': 'unknown', 'reason': 'root-identity-changed'}
        current = os.stat(root.name, dir_fd=parent, follow_symlinks=False)
        if (observed.st_dev, observed.st_ino) != (current.st_dev, current.st_ino):
            return {'status': 'unknown', 'reason': 'root-path-changed'}
        try:
            os.symlink(root.name, '.agent_reports', dir_fd=parent)
        except FileExistsError:
            return {'status': 'preserved', 'reason': 'alias-entry-already-exists'}
        alias = os.stat('.agent_reports', dir_fd=parent, follow_symlinks=False)
        current = os.stat(root.name, dir_fd=parent, follow_symlinks=False)
        parent_now = open_root(root.parent)
        try:
            before_parent, after_parent = os.fstat(parent), os.fstat(parent_now)
            changed = ((observed.st_dev, observed.st_ino) != (current.st_dev, current.st_ino)
                or (before_parent.st_dev, before_parent.st_ino) != (after_parent.st_dev, after_parent.st_ino))
        finally:
            os.close(parent_now)
        if changed:
            current_alias = os.stat('.agent_reports', dir_fd=parent, follow_symlinks=False)
            if (alias.st_dev, alias.st_ino) == (current_alias.st_dev, current_alias.st_ino):
                os.unlink('.agent_reports', dir_fd=parent)
            return {'status': 'unknown', 'reason': 'root-path-changed'}
        return {'status': 'created', 'target': root.name}
    except (OSError, RuntimeError, ValueError, TypeError):
        return {'status': 'unknown', 'reason': 'alias-unavailable'}
    finally:
        if legacy is not None:
            os.close(legacy)
        if parent is not None:
            os.close(parent)
