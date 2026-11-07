"""Read campaign-owned Markdown and select status pages without moving them.

The root alias is resolved by the caller. Every component below that physical
root is opened without following links. Missing ownership is never invented.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import unicodedata

from artifact_nofollow import open_relative, open_root

STATUS_NAMES = ('현황.md', 'status.md')
READ_LIMIT = 65536
LIST_LIMIT = 1000
ROW_LIMIT = 300


def fold(name):
    return unicodedata.normalize('NFC', name).casefold()


def _names(folder):
    fd = open_root(folder)
    try:
        with os.scandir(fd) as entries:
            names = []
            for entry in entries:
                if len(names) >= LIST_LIMIT:
                    raise ValueError('document-list-bounded')
                names.append(entry.name)
        return sorted(names)
    finally:
        os.close(fd)


def status_candidates(folder):
    """All matching names, including unsafe entries that must block a fallback."""
    try:
        return [Path(folder) / name for name in _names(folder) if fold(name) in STATUS_NAMES]
    except FileNotFoundError:
        return []
    except (OSError, RuntimeError, ValueError):
        return None


def read_document(folder, name):
    """Bounded bytes and their fd's stat; no payload pathname is reopened."""
    parent = open_root(folder)
    try:
        fd = open_relative(parent, [name])
    finally:
        os.close(parent)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('document-not-regular')
        chunks, remaining = [], READ_LIMIT + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b''.join(chunks)
        after = os.fstat(fd)
        if (before.st_mtime_ns, before.st_size, before.st_ctime_ns) != (
                after.st_mtime_ns, after.st_size, after.st_ctime_ns):
            raise ValueError('document-changed')
        return raw[:READ_LIMIT], before, 'complete' if len(raw) <= READ_LIMIT else 'partial'
    finally:
        os.close(fd)


def select_status(folder, *, canonical=True, candidates=None):
    """Canonical conflicts/unknown never become an absent page or a legacy choice.

    Legacy directories keep their historical language preference. The caller
    may cache the candidate names, but file contents are observed again.
    """
    paths = status_candidates(folder) if candidates is None else candidates
    result = {'status': 'unknown', 'path': None, 'candidates': paths or []}
    if paths is None:
        return result
    if not paths:
        return dict(result, status='absent')
    paths = sorted(paths, key=lambda path: (STATUS_NAMES.index(fold(path.name)), path.name))
    if not canonical:
        paths = paths[:1]
    try:
        reads = [read_document(Path(folder), path.name) for path in paths]
        if any(coverage != 'complete' for _, _, coverage in reads):
            return result
        for raw, _, _ in reads:
            raw.decode('utf-8')
    except (OSError, RuntimeError, ValueError, UnicodeError):
        return result
    if any(raw != reads[0][0] for raw, _, _ in reads[1:]):
        return dict(result, status='ambiguous')
    return dict(result, status='known', path=paths[0])


def campaign_status(campaign):
    """One campaign's canonical selection, else its legacy cycle pages."""
    primary = select_status(campaign)
    if primary['status'] != 'absent':
        return primary
    import artifact_locator
    pages = []
    try:
        for cycle, _ in artifact_locator.iter_cycle_dirs(Path(campaign)):
            selected = select_status(cycle / 'artifacts', canonical=False)
            if selected['path'] is not None:
                page = selected['path']
                pages.append((page.stat(follow_symlinks=False).st_mtime_ns, str(page), page))
    except (OSError, RuntimeError, ValueError):
        return dict(primary, status='unknown')
    return dict(primary, status='known', path=max(pages)[2]) if pages else primary


def entrance_snapshot(root):
    """Campaign documents join the existing entrance; source failures stay visible."""
    import artifact_identity
    import artifact_lifecycle
    import artifact_locator
    root = Path(root).resolve()
    rows, errors = [], []
    try:
        identity = artifact_lifecycle.read_root_identity(root)
    except Exception:  # malformed optional ownership is unknown, not a new ID
        identity = None
    try:
        campaigns = artifact_locator.iter_campaign_dirs(root)
        for count, campaign in enumerate(campaigns):
            if count >= ROW_LIMIT or len(rows) >= ROW_LIMIT:
                errors.append('campaign-documents-bounded')
                break
            try:
                raw, _, coverage = read_document(campaign, 'campaign.json')
                meta = json.loads(raw) if coverage == 'complete' else {}
                if not isinstance(meta, dict):
                    meta = {}
            except (OSError, RuntimeError, ValueError):
                meta = {}
                errors.append(campaign.relative_to(root).as_posix() + ':campaign-metadata-unreadable')
            campaign_id = meta.get('campaign_id')
            if not artifact_identity.is_well_formed(campaign_id, 'campaign'):
                campaign_id = None
            selected = select_status(campaign)
            if selected['status'] in ('ambiguous', 'unknown'):
                errors.append(campaign.relative_to(root).as_posix() + ':status-' + selected['status'])
            try:
                names = _names(campaign)
            except (OSError, RuntimeError, ValueError):
                errors.append(campaign.relative_to(root).as_posix() + ':documents-unreadable')
                continue
            for name in names:
                if name.startswith('.') or not name.casefold().endswith('.md'):
                    continue
                if len(rows) >= ROW_LIMIT:
                    errors.append('campaign-documents-bounded')
                    break
                try:
                    raw, info, coverage = read_document(campaign, name)
                    try:
                        text = raw.decode('utf-8')
                    except UnicodeDecodeError:
                        text = raw.decode('utf-8', 'replace')
                        coverage = 'partial'
                except (OSError, RuntimeError, ValueError):
                    errors.append((campaign / name).relative_to(root).as_posix() + ':document-unreadable')
                    continue
                title = next((line[2:].strip() for line in text.splitlines() if line.startswith('# ')), name)
                doc = (campaign / name).relative_to(root).as_posix()
                status = selected['status'] if fold(name) in STATUS_NAMES else 'recorded'
                if status in ('known', 'absent'):
                    status = 'recorded'
                if coverage != 'complete':
                    errors.append(doc + ':document-bounded')
                    status = 'unknown'
                rows.append({'kind': 'campaign-document', 'date': datetime.fromtimestamp(
                    info.st_mtime, timezone.utc).strftime('%Y-%m-%d'), 'title': title[:200] or name,
                    'doc_path': doc, 'record_target': doc, 'campaign_id': campaign_id,
                    'artifact_root_id': identity.artifact_root_id if identity else None,
                    'repository_id': identity.repository_id if identity else None,
                    'artifact_id': None, 'revision_id': None, 'status': status,
                    'coverage': coverage, 'mtime': info.st_mtime,
                    'timestamp_source': 'file-mtime', 'result_targets': []})
    except (OSError, RuntimeError, ValueError):
        errors.append('campaign-documents-unreadable')
    return {'kind': 'campaign-document', 'rows': rows,
            'coverage': 'partial' if errors else 'complete', 'errors': errors[:20]}
