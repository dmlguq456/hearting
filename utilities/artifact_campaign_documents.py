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
CAMPAIGN_DOCUMENT = 'campaign.md'
READ_LIMIT = 65536
LIST_LIMIT = 1000
SCAN_LIMIT = 100000
STATUS_PROBES = ('현황.md', 'status.md', 'STATUS.md', 'Status.md')
ROW_LIMIT = 300


def fold(name):
    return unicodedata.normalize('NFC', name).casefold()


def _names(folder, keep=None):
    """Names below ``folder`` that ``keep`` accepts (all when None), sorted.

    The folder is streamed and only accepted names are held, so a campaign
    folder with one cycle directory per route is never "too big" for the few
    files it is asked about. ``SCAN_LIMIT`` bounds the pass itself and
    ``LIST_LIMIT`` the names held; either overrun raises ``document-list-bounded``.
    """
    fd = open_root(folder)
    try:
        with os.scandir(fd) as entries:
            names, seen = [], 0
            for entry in entries:
                seen += 1
                if seen > SCAN_LIMIT:
                    raise ValueError('document-list-bounded')
                if keep is not None and not keep(entry.name):
                    continue
                if len(names) >= LIST_LIMIT:
                    raise ValueError('document-list-bounded')
                names.append(entry.name)
        return sorted(names)
    finally:
        os.close(fd)


def _is_status_name(name):
    return fold(name) in STATUS_NAMES


def _is_document_name(name):
    return not name.startswith('.') and name.casefold().endswith('.md')


def _probe_status(folder):
    """The known spellings of the status pages, by direct lookup, without listing.

    Used only when the folder is too large to list. A link or any other entry
    still counts as a candidate (a lookup never follows it), so an unsafe
    entry blocks a fallback exactly as it does in a listing.
    """
    fd = open_root(folder)
    try:
        found = []
        for name in dict.fromkeys(
                unicodedata.normalize(form, base) for base in STATUS_PROBES for form in ('NFC', 'NFD')):
            try:
                os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            found.append(name)
        return sorted(found)
    finally:
        os.close(fd)


def status_candidates(folder):
    """All matching names, including unsafe entries that must block a fallback.

    A folder too large to list is asked for the known spellings directly. A
    page found that way is a candidate; finding none there cannot prove the
    page absent (another casing may exist), so that stays unknown.
    """
    try:
        try:
            names = _names(folder, _is_status_name)
        except ValueError:
            names = _probe_status(folder)
            if not names:
                return None
        return [Path(folder) / name for name in names]
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


def campaign_page(folder):
    """The campaign folder's own ``campaign.md``, opened by name; None when there is no such name.

    The folder is not listed, so a long folder still shows it. A page that is a link, not a
    regular file, over the read limit or unreadable is the campaign's page that cannot be taken:
    it is ``unknown`` and no older name stands in for it. No ownership marker is required here.
    """
    path = Path(folder) / CAMPAIGN_DOCUMENT
    try:
        # The common case, no page, costs one stat: the no-follow open below walks every path
        # component and is far dearer on a network share. Whatever stands there is opened safely.
        os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        pass
    try:
        raw, _, coverage = read_document(Path(folder), CAMPAIGN_DOCUMENT)
        if coverage == 'complete':
            raw.decode('utf-8')
    except FileNotFoundError:
        return None
    except (OSError, RuntimeError, ValueError, UnicodeError):
        return {'status': 'unknown', 'path': None, 'candidates': [path]}
    if coverage != 'complete':
        return {'status': 'unknown', 'path': None, 'candidates': [path]}
    return {'status': 'known', 'path': path, 'candidates': [path]}


def campaign_status(campaign):
    """One campaign's ``campaign.md``, else its canonical selection, else its legacy cycle pages."""
    page = campaign_page(campaign)
    if page is not None:
        return page
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
                names = _names(campaign, _is_document_name)
            except ValueError:  # too many entries to list: the status pages still show
                errors.append(campaign.relative_to(root).as_posix() + ':documents-unreadable')
                names = [path.name for path in selected['candidates']]
            except (OSError, RuntimeError):
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
