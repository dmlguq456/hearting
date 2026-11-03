from __future__ import annotations

"""D-8/D-9 atomic folder admission.

Effect layer: this module is the only place in rollout step 1 that touches
the filesystem for producer admission. Exclusivity comes from an OS advisory
lock (`flock`) held for the whole admission — process death releases it, so
there is no dead-holder reclamation to race on — never from a rename flag
(F-1/F-2 measured this root does not give no-replace rename). The single
commit point is the canonical publish `atomic_publish.rename_directory_locked`
— see `admit()` step 14.

`.runtime/artifact-admission/v1/index.json` is a derived, rebuildable
accelerator; the durable source of truth is the published manifest and its
events. On a large root its artifact, revision, event and stream rows live in
`index.bulk/` beside it, one file per cycle (see "cycle rows kept beside
index.json" below). `.runtime/artifact-admission/v1/root-identity.json` is the
only value here that cannot be rebuilt from published manifests.
"""

from collections import abc
from contextlib import contextmanager, ExitStack
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import struct
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import artifact_identity
import atomic_publish
import artifact_index
import artifact_locator
import artifact_manifest
import artifact_directory_bundle
import dispatch_lock_order
from artifact_identity import IdAllocator, RootIdentity
from artifact_manifest import Violation

ADMISSION_REL = ".runtime/artifact-admission/v1"
LOCK_TIMEOUT_DEFAULT = 30.0

_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_INDEX_ROW_WARN_COUNT = 5000
_INDEX_BYTE_WARN_SIZE = 2 * 1024 * 1024

_README_TEXT = (
    "This directory is a derived, rebuildable accelerator over published\n"
    "manifests. Do not hand-edit any file here.\n\n"
    "- `index.json` (and `index.bulk/`, its per-cycle rows on a large root) can always be\n"
    "  regenerated: `artifact_admission.rebuild_index()`.\n"
    "- `root-identity.json` is the ONLY value here that cannot be rebuilt.\n"
)


class AdmissionBusy(Exception):
    pass


class AdmissionRecoveryRequired(Exception):
    pass


@dataclass(frozen=True)
class AdmissionRequest:
    idempotency_key: str
    document: Optional[Dict[str, Any]] = None
    staging_source: Optional[Path] = None
    allocator: Optional[IdAllocator] = None


@dataclass(frozen=True)
class AdmissionOutcome:
    status: str
    cycle_path: Optional[str]
    manifest_digest: Optional[str]
    violations: Tuple[Violation, ...]
    index_changed: bool

    def to_payload(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "cycle_path": self.cycle_path,
            "manifest_digest": self.manifest_digest,
            "violations": [v.to_payload() for v in self.violations],
            "index_changed": self.index_changed,
        }


# ---------------------------------------------------------------------------
# low-level fs primitives
# ---------------------------------------------------------------------------


def _admission_dir(root: Path) -> Path:
    return Path(root) / ADMISSION_REL


def _new_token() -> str:
    return "{0}-{1}".format(os.getpid(), uuid.uuid4().hex)


def _atomic_write_bytes(path: Path, data: bytes) -> Optional[List[int]]:
    """Atomically replace ``path`` and return the new file's identity key.

    The return value is ``[dev, ino, size, mtime_ns, mode, ctime_ns]`` taken
    from the still-open temp fd after ``os.replace`` (rename changes ctime), so
    a caller can seed a generation cache with the exact identity a later reader
    will stat.  Existing callers may ignore it.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(".{0}.{1}.tmp".format(path.name, _new_token()))
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    key = None
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
            os.replace(str(temp), str(path))
            # fstat the written file itself: a path stat could observe another
            # writer's later replacement and pair this content with its key.
            try:
                info = os.fstat(fh.fileno())
                key = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                       info.st_mode, info.st_ctime_ns]
            except OSError:
                key = None
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass
    _fsync_dir(path.parent)
    return key


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _create_exclusive_json(path: Path, payload: Dict[str, Any]) -> bool:
    """Write `payload` to `path` only if it does not already exist.

    Returns True if this call created the file, False if it already existed
    (EEXIST) -- caller re-reads in that case.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = artifact_manifest.canonical_bytes(payload)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)
    return True


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(str(path), "rb") as fh:
            return json.loads(fh.read().decode("utf-8"))
    except FileNotFoundError:
        return None


# ---------------------------------------------------------------------------
# lock
# ---------------------------------------------------------------------------


def _lock_dir(root: Path) -> Path:
    # Legacy mkdir-mutex location from the pre-flock implementation. Only read
    # for one-way migration under the flock; never created by current code.
    return _admission_dir(root) / "lock"


def _lock_file_path(root: Path) -> Path:
    return _admission_dir(root) / "lock.flock"


# Per-thread record of which roots (realpath) this thread currently holds the
# producer-admission flock for. `dispatch_lock_order.held()` tracks lock ranks
# but not which root -- a process holding root A's lock and writing root B's
# index would look identical to "no lock held" at the rank level. `holds_lock`
# closes that gap for callers (locator `update_indexes`/`locate`) that must
# never write a root's index without holding *that root's* mutex.
_HELD_ROOTS_STATE = threading.local()


def _held_roots() -> set:
    roots = getattr(_HELD_ROOTS_STATE, "roots", None)
    if roots is None:
        roots = set()
        _HELD_ROOTS_STATE.roots = roots
    return roots


def holds_lock(root: Path) -> bool:
    """Whether this thread currently holds the producer-admission flock for `root`."""
    return str(Path(root).resolve()) in _held_roots()


def current_hold_token() -> int:
    """Identity of this thread's current continuous lock-holding interval.

    The counter grows once per new acquisition, so two loads inside one
    acquisition share a token while a release/re-acquire or another thread gets
    a different one.  Only the seed exception of the index generation cache
    uses it; cache hits outside that interval still require a settled file.
    """
    return getattr(_HELD_ROOTS_STATE, "hold_token", 0)


def try_acquire_lock(root: Path) -> Optional[int]:
    """Non-blocking producer-admission acquire; ``None`` on any contention (never raises).

    Refuses before touching the OS lock when this thread already declares a
    rank at or above ``producer-admission`` -- entering would be a re-entry or
    a reverse-order acquisition, and `dispatch_lock_order.enter` would refuse
    it anyway, but only after this call had already taken and had to release
    the real flock.
    """
    held_ranks = [dispatch_lock_order.RANK[name] for name in dispatch_lock_order.held()
                  if name in dispatch_lock_order.RANK]
    if held_ranks and max(held_ranks) >= dispatch_lock_order.RANK["producer-admission"]:
        return None
    try:
        return _acquire_lock(root, 0.0)
    except (AdmissionBusy, dispatch_lock_order.LockOrderError):
        return None


def _acquire_lock(root: Path, timeout: float, now: Optional[float] = None) -> int:
    """Acquire the admission mutex; returns an fd holding an exclusive flock.

    The mutex is an OS advisory lock (same mechanism as the artifact-root
    pipeline lock), so process death releases it automatically and there is no
    dead-holder reclamation step to race on. The previous mkdir-based mutex
    allowed two waiters to observe the same dead holder and reclaim each
    other's fresh lock; that whole class is structurally gone here.
    """
    lock_path = _lock_file_path(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = (now if now is not None else time.time()) + timeout
    backoff = 0.01
    while True:
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        acquired = False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                acquired = False
            if acquired:
                # The releaser unlinks the file while still holding its flock,
                # so a lock taken on an already-unlinked inode is stale: verify
                # that the path still names the inode this fd locked.
                st_fd = os.fstat(fd)
                try:
                    st_path = os.stat(str(lock_path))
                except FileNotFoundError:
                    st_path = None
                if st_path is not None and (
                    st_path.st_dev,
                    st_path.st_ino,
                ) == (st_fd.st_dev, st_fd.st_ino):
                    # No pre-flock version of this module was ever released, so
                    # there is no legacy mkdir-lock population to migrate; a
                    # stray legacy `lock/` directory is inert diagnostics only.
                    # The lock file stays empty and is unlinked on release: the
                    # flock is the exclusivity, and a rejected admission leaves
                    # every byte of the root unchanged.
                    # PRD §13.53.4(3): declare the acquisition against the
                    # canonical lock-order table. A re-entry or a reverse
                    # acquisition is refused here, typed and immediately,
                    # rather than blocking on `flock` until the admission
                    # timeout turns a contract violation into a "busy" report.
                    batch = getattr(_HELD_ROOTS_STATE, "batch", ())
                    canonical_root = str(Path(root).resolve())
                    if batch and _held_roots():
                        # One sorted multi-root critical section, with one rank.
                        remaining = [item for item in batch if item not in _held_roots()]
                        if not remaining or canonical_root != remaining[0]:
                            raise dispatch_lock_order.LockOrderError("lock-reentry-forbidden", canonical_root)
                    else:
                        dispatch_lock_order.enter("producer-admission")
                    _held_roots().add(canonical_root)
                    _HELD_ROOTS_STATE.hold_token = getattr(
                        _HELD_ROOTS_STATE, "hold_token", 0) + 1
                    result_fd, fd = fd, -1  # ownership transferred to caller
                    return result_fd
        finally:
            if fd >= 0 and not acquired:
                os.close(fd)
            elif fd >= 0 and acquired:
                # acquired but stale inode or busy-raise path: drop this fd.
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(fd)
        if time.time() > deadline:
            raise AdmissionBusy("admission lock busy")
        time.sleep(min(backoff, 0.5))
        backoff = min(backoff * 2, 0.5)


def _release_lock(root: Path, fd: int) -> None:
    _held_roots().discard(str(Path(root).resolve()))
    if not _held_roots():
        dispatch_lock_order.leave("producer-admission")
    # Unlink before unlocking, while exclusivity still holds; a waiter that
    # locked the old inode fails its path/inode verification and retries.
    # Unlink only when the path still names this holder's inode, so a release
    # can never remove a successor's live lock file.
    lock_path = _lock_file_path(root)
    try:
        st_fd = os.fstat(fd)
        st_path = os.stat(str(lock_path))
        if (st_path.st_dev, st_path.st_ino) == (st_fd.st_dev, st_fd.st_ino):
            os.unlink(str(lock_path))
    except OSError:
        pass
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


@contextmanager
def lock_roots(roots, *, now=None):
    """One admission critical section over distinct roots in realpath order.

    Ordinary lock reentry remains forbidden. The batch owns the whole rank
    until its last root is released, including an interrupted acquisition.
    """
    if _held_roots() or getattr(_HELD_ROOTS_STATE, "batch", ()):
        raise dispatch_lock_order.LockOrderError("lock-reentry-forbidden", "producer-admission")
    ordered = tuple(sorted({str(Path(root).resolve()) for root in roots}))
    _HELD_ROOTS_STATE.batch = ordered
    try:
        with ExitStack() as locks:
            for item in ordered:
                root = Path(item)
                fd = _acquire_lock(root, LOCK_TIMEOUT_DEFAULT, now=now)
                locks.callback(_release_lock, root, fd)
            yield
    finally:
        _HELD_ROOTS_STATE.batch = ()


def force_release_lock(root: Path) -> None:
    """Operator escape hatch for legacy mkdir-lock remnants only.

    The flock mutex releases itself on process death, so there is nothing to
    force there; this clears a stale legacy lock directory and the diagnostic
    payload. Not called automatically.
    """
    legacy_dir = _lock_dir(root)
    try:
        (legacy_dir / "holder.json").unlink()
    except OSError:
        pass
    try:
        os.rmdir(str(legacy_dir))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# publish plan
# ---------------------------------------------------------------------------


def _path_under_root(root: Path, candidate: Path) -> Path:
    """Return candidate only when existing parent symlinks cannot escape root."""

    try:
        root_resolved = Path(root).resolve(strict=False)
        candidate.resolve(strict=False).relative_to(root_resolved)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("publish-path-outside-artifact-root") from exc
    return candidate


def publish_plan(root: Path, campaign_id: str, cycle_id: str) -> Tuple[Path, Path, Optional[Path]]:
    root = Path(root)
    campaigns_dir = _path_under_root(root, root / "campaigns")
    campaign_dir = _path_under_root(root, campaigns_dir / campaign_id)
    cycles_dir = _path_under_root(root, campaign_dir / "cycles")
    cycle_dir = _path_under_root(root, cycles_dir / cycle_id)

    if not campaigns_dir.exists():
        publish_target, parent = campaigns_dir, root
    elif not campaign_dir.exists():
        publish_target, parent = campaign_dir, campaigns_dir
    elif not cycles_dir.exists():
        publish_target, parent = cycles_dir, campaign_dir
    elif not cycle_dir.exists():
        publish_target, parent = cycle_dir, cycles_dir
    else:
        return cycle_dir, cycles_dir, None

    token = _new_token()
    staging = parent / (".admitting-" + token)
    return publish_target, parent, staging


# ---------------------------------------------------------------------------
# staging
# ---------------------------------------------------------------------------


def _stage_tree(
    staging: Path,
    publish_target: Path,
    cycle_dir: Path,
    document: Dict[str, Any],
    staging_source: Optional[Path],
) -> Path:
    """Build `staging` as the future contents of `publish_target`.

    Returns the absolute path, inside `staging`, corresponding to the cycle
    directory (where manifest.json and declared files live).
    """
    rel = os.path.relpath(str(cycle_dir), str(publish_target))
    cycle_content_dir = staging if rel == "." else staging / rel
    os.makedirs(str(cycle_content_dir))

    manifest_bytes = artifact_manifest.canonical_bytes(document)
    manifest_path = cycle_content_dir / "manifest.json"
    fd = os.open(str(manifest_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(manifest_bytes)
        fh.flush()
        os.fsync(fh.fileno())

    if staging_source is not None:
        paths = {row[0] for row in artifact_manifest.declared_locators(document)
                 if isinstance(row[0], str)}
        coverage, _ = _bundle_coverage(Path(staging_source), document)
        paths.update(coverage)
        copied = set()
        for path_value in sorted(paths):
            if not isinstance(path_value, str):
                continue
            if path_value in copied:
                continue
            src = Path(staging_source) / path_value
            dst = cycle_content_dir / path_value
            dst.parent.mkdir(parents=True, exist_ok=True)
            if os.path.islink(str(src)) or not os.path.isfile(str(src)):
                continue  # surfaced as a violation by _verify_staged_files
            try:
                artifact_directory_bundle.copy_file_streaming(src, dst)
            except artifact_directory_bundle.DirectoryBundleError:
                # Preserve the admission API's structured rejection path;
                # staged verification reports the missing or mismatched row.
                continue
            copied.add(path_value)

    for dirpath, _dirnames, _filenames in os.walk(str(staging)):
        _fsync_dir(Path(dirpath))

    return cycle_content_dir


def _walk_all_entries(top: Path):
    for dirpath, dirnames, filenames in os.walk(str(top)):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            yield Path(full)


def _bundle_coverage(cycle_root: Path, document: Dict[str, Any]):
    """Return validated directory-bundle leaves and typed violations."""
    artifacts = {row.get("artifact_id"): row for row in document.get("artifacts", [])
                 if isinstance(row, Mapping)}
    paths = set()
    violations = []
    directories = []
    for row in document.get("artifact_revisions", []) or []:
        if not isinstance(row, Mapping):
            continue
        artifact = artifacts.get(row.get("artifact_id"), {})
        media = row.get("media_type")
        if artifact.get("type") != "directory-bundle" and media != artifact_directory_bundle.MEDIA_TYPE:
            continue
        locator = row.get("locator")
        rel = locator.get("path") if isinstance(locator, Mapping) else None
        if artifact.get("type") != "directory-bundle" or media != artifact_directory_bundle.MEDIA_TYPE or not isinstance(rel, str):
            violations.append(Violation("bundle-type-media-mismatch", str(rel or "$"), "bundle declaration mismatch"))
            continue
        try:
            covered = artifact_directory_bundle.verify_coverage(cycle_root, rel, row)
            body = artifact_directory_bundle.parse_descriptor((cycle_root / rel).read_bytes())
            directory = body["directory"]["path"]
            if artifacts.get(row.get("artifact_id"), {}).get("title") != directory[len("artifacts/"):]:
                violations.append(Violation("bundle-title-directory-mismatch", rel, "bundle title does not match body"))
                continue
            if any(directory == prior or directory.startswith(prior + "/")
                   or prior.startswith(directory + "/") for prior in directories):
                violations.append(Violation("bundle-directory-overlap", directory, "overlapping directory bundles"))
                continue
            directories.append(directory)
            paths.update(covered)
        except artifact_directory_bundle.DirectoryBundleError as exc:
            violations.append(Violation(exc.code, exc.path or rel, exc.detail))
    return paths, violations


def _verify_staging_source(
    staging_source: Optional[Path], document: Dict[str, Any]
) -> List[Violation]:
    """Refuse an unsafe caller-supplied staging source.

    `_stage_tree` copies only the declared locator paths, so an unsafe entry in
    the source would otherwise be silently dropped instead of refused. D-8.2
    validates the candidate before anything is admitted, so the source tree is
    held to the same rules as the staging tree it seeds: no symlinks, no
    non-regular files, and no file that no locator declares.
    """
    violations = []  # type: List[Violation]
    if staging_source is None:
        return violations
    source = Path(staging_source)
    if not source.is_dir():
        return violations

    declared_paths = set()
    for path_value, _digest, _size, _media in artifact_manifest.declared_locators(document):
        if isinstance(path_value, str):
            declared_paths.add(path_value)
    coverage, coverage_violations = _bundle_coverage(source, document)
    violations.extend(coverage_violations)
    declared_paths.update(coverage)

    for entry in _walk_all_entries(source):
        rel = os.path.relpath(str(entry), str(source)).replace(os.sep, "/")
        if os.path.islink(str(entry)):
            violations.append(
                Violation("staging-symlink-forbidden", rel, "symlink in staging source")
            )
            continue
        if entry.is_dir():
            continue
        if not entry.is_file():
            violations.append(
                Violation(
                    "staging-non-regular-file-forbidden",
                    rel,
                    "non-regular file in staging source",
                )
            )
            continue
        root_manifest = artifact_manifest.classify_artifact_path(
            str(source), None, "", "control", rel, "regular",
        ).allowed
        if not root_manifest and rel not in declared_paths:
            violations.append(
                Violation(
                    "staging-undeclared-extra-file",
                    rel,
                    "file not declared by any locator",
                )
            )
    return violations


def _verify_staged_files(
    cycle_content_dir: Path, document: Dict[str, Any]
) -> List[Violation]:
    violations: List[Violation] = []

    for entry in _walk_all_entries(cycle_content_dir):
        if os.path.islink(str(entry)):
            violations.append(
                Violation("staging-symlink-forbidden", str(entry), "symlink in staging")
            )
            continue
        if entry.is_dir():
            continue
        if not entry.is_file():
            violations.append(
                Violation(
                    "staging-non-regular-file-forbidden",
                    str(entry),
                    "non-regular file in staging",
                )
            )

    declared = artifact_manifest.declared_locators(document)
    coverage, coverage_violations = _bundle_coverage(cycle_content_dir, document)
    violations.extend(coverage_violations)
    declared_paths = set()
    for path_value, digest, size, _media in declared:
        if not isinstance(path_value, str):
            continue
        declared_paths.add(path_value)
        full = cycle_content_dir / path_value
        if os.path.islink(str(full)):
            continue  # already reported above
        if not full.exists():
            violations.append(
                Violation("staging-missing-declared-file", path_value, "declared file not staged")
            )
            continue
        if not full.is_file():
            continue  # already reported above
        actual_size = full.stat().st_size
        if isinstance(size, int) and actual_size != size:
            violations.append(
                Violation(
                    "staging-byte-size-mismatch",
                    path_value,
                    "expected {0}, got {1}".format(size, actual_size),
                )
            )
        if isinstance(digest, str):
            hasher = hashlib.sha256()
            with open(full, "rb") as stream:
                while True:
                    chunk = stream.read(artifact_directory_bundle.CHUNK_BYTES)
                    if not chunk:
                        break
                    hasher.update(chunk)
            actual_digest = "sha256:" + hasher.hexdigest()
            if actual_digest != digest:
                violations.append(
                    Violation(
                        "staging-digest-mismatch",
                        path_value,
                        "expected {0}, got {1}".format(digest, actual_digest),
                    )
                )
    declared_paths.update(coverage)

    all_files = set()
    for entry in _walk_all_entries(cycle_content_dir):
        if entry.is_file() and not os.path.islink(str(entry)):
            rel = os.path.relpath(str(entry), str(cycle_content_dir))
            rel = rel.replace(os.sep, "/")
            root_manifest = artifact_manifest.classify_artifact_path(
                str(cycle_content_dir), None, "", "control", rel, "regular",
            ).allowed
            if root_manifest:
                continue
            all_files.add(rel)
    extra = all_files - declared_paths
    for path_value in sorted(extra):
        violations.append(
            Violation("staging-undeclared-extra-file", path_value, "file not declared by any locator")
        )

    return violations


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    for dirpath, dirnames, filenames in os.walk(str(path), topdown=False):
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                if os.path.islink(full):
                    os.unlink(full)
                else:
                    os.chmod(full, 0o700)
                    os.unlink(full)
            except OSError:
                pass
        for name in dirnames:
            full = os.path.join(dirpath, name)
            try:
                os.rmdir(full)
            except OSError:
                pass
    try:
        os.rmdir(str(path))
    except OSError:
        pass


def _quarantine(root: Path, staging: Path, idempotency_key: str) -> Path:
    quarantine_dir = _admission_dir(root) / "quarantine"
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    while True:
        dest = quarantine_dir / "{0}-{1}".format(idempotency_key, n)
        try:
            atomic_publish.rename_directory_locked(staging, dest)
            return dest
        except FileExistsError:
            n += 1
        except FileNotFoundError:
            return dest


# ---------------------------------------------------------------------------
# journal
# ---------------------------------------------------------------------------


def _journal_dir(root: Path) -> Path:
    return _admission_dir(root) / "journal"


def _journal_path(root: Path, idempotency_key: str) -> Path:
    return _journal_dir(root) / "{0}.json".format(idempotency_key)


def _write_journal(root: Path, idempotency_key: str, **fields: Any) -> None:
    path = _journal_path(root, idempotency_key)
    payload = dict(fields)
    payload["idempotency_key"] = idempotency_key
    _atomic_write_bytes(path, artifact_manifest.canonical_bytes(payload))


def _read_journals(root: Path) -> List[Dict[str, Any]]:
    journal_dir = _journal_dir(root)
    if not journal_dir.exists():
        return []
    out = []
    for name in sorted(os.listdir(str(journal_dir))):
        if not name.endswith(".json"):
            continue
        data = _read_json(journal_dir / name)
        if data is not None:
            out.append(data)
    return out


def _remove_journal(root: Path, idempotency_key: str) -> None:
    path = _journal_path(root, idempotency_key)
    try:
        path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# root identity
# ---------------------------------------------------------------------------


def _root_identity_path(root: Path) -> Path:
    return _admission_dir(root) / "root-identity.json"


def ensure_root_identity(
    root: Path, *, allocator: Optional[IdAllocator] = None, now: Optional[float] = None
) -> RootIdentity:
    root = Path(root)
    path = _root_identity_path(root)
    existing = _read_json(path)
    if existing is not None:
        return RootIdentity.parse(existing)

    alloc = allocator if allocator is not None else IdAllocator()
    issued_at = _rfc3339(now)
    payload = RootIdentity(
        schema_version=1,
        artifact_root_id=alloc.allocate("artifact_root"),
        repository_id=alloc.allocate("repository"),
        issued_at=issued_at,
        producer_contract_version=artifact_manifest.CONTRACT_VERSION,
    ).to_payload()

    created = _create_exclusive_json(path, payload)
    readme_path = _admission_dir(root) / "README.md"
    if not readme_path.exists():
        try:
            fd = os.open(str(readme_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(_README_TEXT)
        except FileExistsError:
            pass

    if created:
        return RootIdentity.parse(payload)
    # a competitor issued it first; read what they wrote (never overwrite).
    reread = _read_json(path)
    return RootIdentity.parse(reread)


def _rfc3339(now: Optional[float]) -> str:
    t = now if now is not None else time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + "Z"


# ---------------------------------------------------------------------------
# index load / rebuild / verify
# ---------------------------------------------------------------------------


def _index_path(root: Path) -> Path:
    return _admission_dir(root) / "index.json"


def _index_head_path(root: Path) -> Path:
    return _admission_dir(root) / "index.json.head.json"


# ---------------------------------------------------------------------------
# index generation cache and head sidecar
#
# `index.json` is a derived document: every writer replaces it atomically, so
# one exact stat identity denotes one generation.  Two derived layers avoid
# re-parsing it: a one-generation in-process cache (seedable only while the
# writer holds this root's admission lock) and `index.json.head.json`, a small
# cross-process projection of the same generation.  Both are accelerators: a
# missing, stale, malformed or unsettled key simply falls back to the full
# `_read_json` + `_parse_owned` path that existed before them.
# ---------------------------------------------------------------------------

_INDEX_SETTLE_NS = 100_000_000
_INDEX_CACHE_LOCK = threading.RLock()
_INDEX_GENERATION: Optional[Dict[str, Any]] = None
_HEAD_WRITES: Dict[str, tuple] = {}


@dataclass(frozen=True)
class IndexHead:
    """A validated index generation projection for absence/existence questions.

    Deliberately not an ``IndexDocument``: consumers can only ask about the
    sections they were moved to, never accidentally treat it as the full index.
    """

    key: Tuple[int, ...]
    artifact_root_id: str
    schema_version: int
    manifests: Mapping[str, Mapping[str, Any]]
    cycles: Mapping[str, Mapping[str, Any]]
    routes: Mapping[str, Mapping[str, Mapping[str, Any]]]
    prefixed_stable_ids: Mapping[str, frozenset]

    def has_prefixed_stable_id(self, value: Any) -> bool:
        if not isinstance(value, str):
            return False
        for prefix, identifiers in self.prefixed_stable_ids.items():
            if value.startswith(prefix):
                return value in identifiers
        return False


def _file_key(path: Path) -> Optional[List[int]]:
    try:
        info = os.stat(str(path))
    except (OSError, ValueError):
        return None
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_mode, info.st_ctime_ns]


def _key_settled(key: Optional[List[int]]) -> bool:
    return key is None or time.time_ns() - key[3] > _INDEX_SETTLE_NS


def _copy_index_document(document: artifact_index.IndexDocument) -> artifact_index.IndexDocument:
    """Shallow-copy every section so a caller's section edits never reach a cache."""
    return artifact_index.IndexDocument(
        schema_version=document.schema_version,
        artifact_root_id=document.artifact_root_id,
        stable_ids=artifact_index.copy_section(document.stable_ids),
        routes={name: dict(bucket) for name, bucket in document.routes.items()},
        event_ids=artifact_index.copy_section(document.event_ids),
        streams=artifact_index.copy_section(document.streams),
        manifests=dict(document.manifests),
        cycles=dict(document.cycles),
    )


def _generation_hit_allowed(root: Path, cached: Mapping[str, Any]) -> bool:
    if _key_settled(cached.get("key")):
        return True
    # The only exception: the continuous lock-holding interval that seeded it.
    return (cached.get("hold_token") == current_hold_token()
            and cached.get("thread") == threading.get_ident()
            and holds_lock(root))


def load_index(root: Path) -> artifact_index.IndexDocument:
    global _INDEX_GENERATION
    root = Path(root)
    path = _index_path(root)
    resolved = os.path.realpath(str(path))
    key = _file_key(path)
    if key is not None:
        with _INDEX_CACHE_LOCK:
            cached = _INDEX_GENERATION
        if (cached is not None and cached.get("path") == resolved
                and cached.get("key") == key and _generation_hit_allowed(root, cached)):
            return _copy_index_document(cached["document"])
    payload = _read_json(path)
    store = _bulk_store(root)
    if payload is None:
        identity = ensure_root_identity(root)
        empty = artifact_index.empty(identity.artifact_root_id)
        return empty if store is None else _with_bulk(empty, store)
    document = artifact_index._parse_owned(payload)
    if store is not None:
        document = _with_bulk(document, store)
    key_after = _file_key(path)
    if key is not None and key_after == key:
        # A reader that validated this generation may leave the small head
        # projection for the next process; a later index write invalidates it
        # by key, so this needs no lock.
        _write_index_head(root, document, key)
    if key is not None and key_after == key and _key_settled(key):
        with _INDEX_CACHE_LOCK:
            _INDEX_GENERATION = {"path": resolved, "key": key,
                                 "document": _copy_index_document(document)}
    return document


def clear_index_cache() -> None:
    """Test/administrative helper: drop the one-generation process cache."""
    global _INDEX_GENERATION
    with _INDEX_CACHE_LOCK:
        _INDEX_GENERATION = None


def _head_payload(root: Path, index: artifact_index.IndexDocument, key: List[int]) -> Dict[str, Any]:
    prefixes = (artifact_identity.prefix_for("campaign"), artifact_identity.prefix_for("cycle"))
    pinned = {prefix: [] for prefix in prefixes}
    stable = index.stable_ids
    for stable_id in (stable.kept() if isinstance(stable, _BulkSection) else stable):
        if not isinstance(stable_id, str):
            continue
        for prefix in prefixes:
            if stable_id.startswith(prefix):
                pinned[prefix].append(stable_id)
                break
    return {"schema": 1, "key": list(key), "artifact_root_id": index.artifact_root_id,
            "schema_version": index.schema_version,
            "manifests": dict(index.manifests), "cycles": dict(index.cycles),
            "routes": {name: dict(bucket) for name, bucket in index.routes.items()},
            "prefixed_stable_ids": {prefix: sorted(ids) for prefix, ids in pinned.items()}}


def _write_index_head(root: Path, index: artifact_index.IndexDocument, key: List[int]) -> None:
    try:
        head_path = _index_head_path(root)
        payload = _head_payload(root, index, key)
        written = _atomic_write_bytes(head_path, artifact_manifest.canonical_bytes(payload))
        if written is not None:
            with _INDEX_CACHE_LOCK:
                _HEAD_WRITES[os.path.realpath(str(head_path))] = (
                    tuple(written), current_hold_token(), threading.get_ident())
                if len(_HEAD_WRITES) > 16:
                    _HEAD_WRITES.pop(next(iter(_HEAD_WRITES)))
    except Exception:  # noqa: BLE001 - the head is a derived accelerator
        pass


def _parse_index_head(payload: Any) -> Optional[IndexHead]:
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        return None
    key = payload.get("key")
    if (not isinstance(key, list) or len(key) != 6
            or not all(isinstance(item, int) and not isinstance(item, bool) for item in key)):
        return None
    root_id = payload.get("artifact_root_id")
    schema_version = payload.get("schema_version")
    if (not isinstance(root_id, str) or not root_id or not isinstance(schema_version, int)
            or isinstance(schema_version, bool)):
        return None
    sections = {}
    for name, shapes in (("manifests", artifact_index._ROW_SHAPES["manifests"]),
                         ("cycles", artifact_index._ROW_SHAPES["cycles"])):
        rows = payload.get(name)
        if not isinstance(rows, dict):
            return None
        for row_key, row in rows.items():
            artifact_index._check_row_shape(name, row_key, row, shapes)
        sections[name] = rows
    routes = payload.get("routes")
    if not isinstance(routes, dict):
        return None
    copied_routes = {}
    for root_key, bucket in routes.items():
        if not isinstance(root_key, str) or not isinstance(bucket, dict):
            return None
        copied = {}
        for route_key, row in bucket.items():
            artifact_index._check_row_shape("routes", route_key, row, artifact_index._ROUTE_ROW_SHAPE)
            copied[route_key] = row
        copied_routes[root_key] = copied
    pinned = payload.get("prefixed_stable_ids")
    if not isinstance(pinned, dict):
        return None
    prefixed = {}
    for prefix in (artifact_identity.prefix_for("campaign"), artifact_identity.prefix_for("cycle")):
        values = pinned.get(prefix)
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            return None
        prefixed[prefix] = frozenset(values)
    return IndexHead(key=tuple(key), artifact_root_id=root_id, schema_version=schema_version,
                     manifests=sections["manifests"], cycles=sections["cycles"],
                     routes=copied_routes, prefixed_stable_ids=prefixed)


def load_index_head(root: Path) -> Optional[IndexHead]:
    """The current generation's head projection, or None to use the full index."""
    root = Path(root)
    index_path = _index_path(root)
    head_path = _index_head_path(root)
    key = _file_key(index_path)
    if key is None:
        return None
    try:
        payload = json.loads(head_path.read_bytes().decode("utf-8"))
        head = _parse_index_head(payload)
    except (OSError, ValueError, UnicodeError, TypeError, KeyError, AttributeError):
        return None
    if head is None or list(head.key) != key or _file_key(index_path) != key:
        return None
    if not _key_settled(key):
        with _INDEX_CACHE_LOCK:
            written = _HEAD_WRITES.get(os.path.realpath(str(head_path)))
        if not (written == (tuple(key), current_hold_token(), threading.get_ident())
                and holds_lock(root)):
            return None
    return head


# ---------------------------------------------------------------------------
# cycle rows kept beside index.json (3366fe)
#
# Above `BULK_THRESHOLD_ROWS` rows, the three sections that grow with every
# artifact and event -- `stable_ids` rows of artifacts, artifact revisions and
# events, `event_ids` and `streams` -- leave `index.json` for one file per cycle
# under `index.bulk/cycles/`, with `index.bulk/keys.bin`, a sorted table that
# names the cycle file holding each key.  `index.json` keeps the same closed
# shape with every other row (manifests, cycles, routes, campaign and cycle ids,
# ...), so a resident helper of an older release still reads and writes it: it
# sees no moved rows (its own uniqueness check covers fewer IDs; it never
# refuses on their absence, because a cycle's events and stream cursors leave
# together), and the rows it adds stay in `index.json` until the next writer of
# this release folds them into the cycle files and leaves them out again.
#
# Moved rows are only ever added: a row names its key's owner and is immutable,
# and a stream cursor only moves forward (the higher one is kept).  A writer
# therefore rewrites only the cycle files its change touches and appends the
# keys it adds to `keys.bin`, each by atomic replace, before `index.json`;
# a crash in between leaves rows a later read already sees, never loses one.
# Rows still in `index.json` (all of them when the layout is turned on, or an
# older writer's) move a whole cycle at a time, at most `BULK_FOLD_ROWS` per
# write beyond the cycles the write changes, so no single write holds the lock
# for the whole move and an older reader always sees each cycle's rows either
# all together or not at all.  `format.json` turns the layout on; without it the
# directory is ignored.  Rebuild (and a writer handing in a whole document)
# rewrites every cycle file.
# ---------------------------------------------------------------------------

BULK_REL = "index.bulk"
BULK_THRESHOLD_ROWS = 50_000
BULK_FOLD_ROWS = 50_000
_BULK_SECTIONS = ("stable_ids", "event_ids", "streams")
_BULK_STABLE_PREFIXES = tuple(artifact_identity.prefix_for(kind)
                              for kind in ("artifact", "artifact_revision", "event"))
_BULK_OTHER = "_other"
_BULK_MAGIC = b"HTGKEYS1"
_BULK_HEADER = struct.Struct(">8sII")
_BULK_ENTRY = struct.Struct(">QI")
_BULK_NAME_BYTES = 36


def _bulk_dir(root: Path) -> Path:
    return _admission_dir(root) / BULK_REL


def _bulk_moved(section: str, key: Any) -> bool:
    return section != "stable_ids" or (isinstance(key, str) and key.startswith(_BULK_STABLE_PREFIXES))


def _bulk_key_hash(key: str) -> int:
    return int.from_bytes(hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest(), "big")


def _bulk_owner(row: Any) -> str:
    cycle_id = row.get("cycle_id") if type(row) is dict else None
    return cycle_id if type(cycle_id) is str and _CYCLE_ID_FILE_RE.match(cycle_id) else _BULK_OTHER


def _bulk_merge_row(section: str, older: Any, newer: Any) -> Any:
    """Rows are immutable once written (the newer wins, as a dict assignment would); a stream
    cursor only moves forward, so the higher `last_sequence` wins."""
    if older is None:
        return newer
    if newer is None:
        return older
    if section == "streams":
        try:
            if int(older.get("last_sequence", 0)) > int(newer.get("last_sequence", 0)):
                return older
        except (TypeError, ValueError, AttributeError):
            pass
    return newer


def _bulk_empty_sections() -> Dict[str, Dict[str, Any]]:
    return {name: {} for name in _BULK_SECTIONS}


class _BulkLocator:
    """`keys.bin`: a sorted (key hash, cycle-file number) table and the cycle-file names."""

    def __init__(self, names: List[str], data: bytes, offset: int, count: int):
        self.names = names
        self.data = data
        self.offset = offset
        self.count = count

    @classmethod
    def empty(cls) -> "_BulkLocator":
        return cls([], b"", 0, 0)

    @classmethod
    def parse(cls, data: bytes) -> "_BulkLocator":
        if len(data) < _BULK_HEADER.size:
            raise ValueError("bulk-locator-short")
        magic, names_count, count = _BULK_HEADER.unpack_from(data, 0)
        offset = _BULK_HEADER.size + names_count * _BULK_NAME_BYTES
        if magic != _BULK_MAGIC or len(data) != offset + count * _BULK_ENTRY.size:
            raise ValueError("bulk-locator-invalid")
        names = [data[_BULK_HEADER.size + i * _BULK_NAME_BYTES:
                      _BULK_HEADER.size + (i + 1) * _BULK_NAME_BYTES].decode("ascii").rstrip()
                 for i in range(names_count)]
        return cls(names, data, offset, count)

    def _hash_at(self, position: int) -> int:
        return _BULK_ENTRY.unpack_from(self.data, self.offset + position * _BULK_ENTRY.size)[0]

    def _lower(self, value: int) -> int:
        low, high = 0, self.count
        while low < high:
            middle = (low + high) // 2
            if self._hash_at(middle) < value:
                low = middle + 1
            else:
                high = middle
        return low

    def owners(self, key: str) -> List[str]:
        return self._owners_of(_bulk_key_hash(key))

    def _owners_of(self, value: int) -> List[str]:
        position = self._lower(value)
        found = []
        while position < self.count:
            hashed, number = _BULK_ENTRY.unpack_from(self.data, self.offset + position * _BULK_ENTRY.size)
            if hashed != value:
                break
            if number < len(self.names):
                found.append(self.names[number])
            position += 1
        return found

    def with_entries(self, pairs) -> Optional["_BulkLocator"]:
        """This table plus `(key, cycle-file name)` pairs it lacks, or None when it has them all."""
        names = list(self.names)
        numbers = {name: number for number, name in enumerate(names)}
        pairs = list(pairs)
        if len(pairs) * 16 > self.count:
            # Many keys at once (a cycle moved out of index.json): one merge of the whole table.
            for _key, owner in pairs:
                if owner not in numbers:
                    numbers[owner] = len(names)
                    names.append(owner)
            entries = list(_BULK_ENTRY.iter_unpack(self.data[self.offset:]))
            entries.extend((_bulk_key_hash(key), numbers[owner]) for key, owner in pairs)
            entries.sort()
            unique = [entry for position, entry in enumerate(entries)
                      if position == 0 or entries[position - 1] != entry]
            if len(unique) == self.count and len(names) == len(self.names):
                return None
            return _BulkLocator.build(names, b"".join(_BULK_ENTRY.pack(*entry) for entry in unique), len(unique))
        missing = set()
        for key, owner in pairs:
            value = _bulk_key_hash(key)
            if owner in self._owners_of(value):
                continue
            if owner not in numbers:
                numbers[owner] = len(names)
                names.append(owner)
            missing.add((value, numbers[owner]))
        if not missing:
            return None
        pieces, cursor = [], 0
        start = self.offset
        for value, number in sorted(missing):
            position = self._lower(value)
            pieces.append(self.data[start + cursor * _BULK_ENTRY.size: start + position * _BULK_ENTRY.size])
            pieces.append(_BULK_ENTRY.pack(value, number))
            cursor = position
        pieces.append(self.data[start + cursor * _BULK_ENTRY.size: start + self.count * _BULK_ENTRY.size])
        return _BulkLocator.build(names, b"".join(pieces), self.count + len(missing))

    @staticmethod
    def build(names: List[str], entries: bytes, count: int) -> "_BulkLocator":
        header = _BULK_HEADER.pack(_BULK_MAGIC, len(names), count)
        table = b"".join(name.encode("ascii").ljust(_BULK_NAME_BYTES) for name in names)
        data = header + table + entries
        return _BulkLocator(names, data, len(header) + len(table), count)

    @staticmethod
    def full(owner_keys: Mapping[str, Any]) -> "_BulkLocator":
        return _BulkLocator.empty().with_entries(
            (key, name) for name in sorted(owner_keys) for key in owner_keys[name]) or _BulkLocator.empty()


class _BulkStore:
    """One root's cycle files and key table, read lazily and kept for this process."""

    def __init__(self, root: Path):
        self.dir = _bulk_dir(root)
        self.real = os.path.realpath(str(self.dir))
        self._locator: Optional[Tuple[Optional[List[int]], _BulkLocator]] = None
        self._derived = False  # the table in hand was derived from the cycle files, not read
        self._shards: Dict[str, Tuple[Optional[List[int]], Dict[str, Dict[str, Any]]]] = {}

    def _locator_path(self) -> Path:
        return self.dir / "keys.bin"

    def _shard_path(self, name: str) -> Path:
        return self.dir / "cycles" / "{0}.json".format(name)

    def locator(self, *, fresh: bool = False) -> _BulkLocator:
        """The key table every lookup follows.  A table that is missing or does not parse is derived
        from the cycle files it indexes (they are the rows; the table only finds them), so a lookup
        never misses a row a cycle file holds; the next locked write stores the derived table."""
        path = self._locator_path()
        if self._locator is not None and not fresh:
            return self._locator[1]
        key = _file_key(path)
        if self._locator is not None and self._locator[0] == key and (key is not None or self._derived):
            return self._locator[1]
        try:
            locator = _BulkLocator.parse(path.read_bytes())
            self._derived = False
        except (FileNotFoundError, ValueError):
            locator = _BulkLocator.full({name: self.keys_of(name) for name in self.names()})
            self._derived = True
        self._locator = (key, locator)
        return locator

    def keys_of(self, name: str) -> List[str]:
        shard = self.shard(name)
        return [key for section in _BULK_SECTIONS for key in shard[section]]

    def uncovered(self) -> List[Tuple[str, str]]:
        """(cycle file, key) pairs the key table in use does not name: lookups would miss them."""
        locator = self.locator(fresh=True)
        numbers = {name: number for number, name in enumerate(locator.names)}
        entries = set(_BULK_ENTRY.iter_unpack(locator.data[locator.offset:]))
        return [(name, key) for name in self.names() for key in self.keys_of(name)
                if (_bulk_key_hash(key), numbers.get(name, -1)) not in entries]

    def shard(self, name: str, *, fresh: bool = False) -> Dict[str, Dict[str, Any]]:
        cached = self._shards.get(name)
        if cached is not None and not fresh:
            return cached[1]
        path = self._shard_path(name)
        key = _file_key(path)
        if cached is not None and key is not None and cached[0] == key:
            return cached[1]
        payload = _read_json(path) if key is not None else None
        sections = _bulk_empty_sections()
        if isinstance(payload, dict):
            for section in _BULK_SECTIONS:
                rows = payload.get(section)
                if isinstance(rows, dict):
                    sections[section] = rows
        self._shards[name] = (key, sections)
        return sections

    def get(self, section: str, key: str) -> Any:
        found = None
        for name in self.locator().owners(key):
            found = _bulk_merge_row(section, found, self.shard(name)[section].get(key))
        return found

    def names(self) -> List[str]:
        try:
            return sorted(name[:-5] for name in os.listdir(str(self.dir / "cycles"))
                          if name.endswith(".json") and not name.startswith("."))
        except FileNotFoundError:
            return []

    def everything(self, section: str) -> Dict[str, Any]:
        rows: Dict[str, Any] = {}
        for name in self.names():
            for key, row in self.shard(name)[section].items():
                rows[key] = _bulk_merge_row(section, rows.get(key), row)
        return rows

    def _write_shard(self, name: str, sections: Dict[str, Dict[str, Any]]) -> None:
        """Atomic replace of one cycle file; the caller syncs the directory once for the batch."""
        payload = {"cycle": name, "schema": 1}
        payload.update(sections)
        path = self._shard_path(name)
        temp = path.with_name(".{0}.{1}.tmp".format(path.name, _new_token()))
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(artifact_manifest.canonical_bytes(payload))
                handle.flush()
                os.fsync(handle.fileno())
                os.replace(str(temp), str(path))
                info = os.fstat(handle.fileno())
        except BaseException:
            try:
                os.unlink(str(temp))
            except OSError:
                pass
            raise
        self._shards[name] = ([info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                               info.st_mode, info.st_ctime_ns], sections)

    def _write_locator(self, locator: _BulkLocator) -> None:
        key = _atomic_write_bytes(self._locator_path(), locator.data)
        self._locator = (key, locator)
        self._derived = False

    def add(self, rows: Mapping[str, Mapping[str, Any]]) -> None:
        """Fold `rows` into the cycle files that own them, then name their keys in `keys.bin`."""
        by_owner: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for section in _BULK_SECTIONS:
            for key, row in (rows.get(section) or {}).items():
                by_owner.setdefault(_bulk_owner(row), _bulk_empty_sections())[section][key] = row
        pairs = []
        (self.dir / "cycles").mkdir(parents=True, exist_ok=True)
        wrote = False
        for name, incoming in sorted(by_owner.items()):
            current = self.shard(name, fresh=True)
            merged = {section: dict(current[section]) for section in _BULK_SECTIONS}
            changed = False
            for section in _BULK_SECTIONS:
                for key, row in incoming[section].items():
                    value = _bulk_merge_row(section, merged[section].get(key), row)
                    if merged[section].get(key) != value:
                        merged[section][key] = value
                        changed = True
                    pairs.append((key, name))
            if changed:
                self._write_shard(name, merged)
                wrote = True
        if wrote:
            _fsync_dir(self.dir / "cycles")
        current = self.locator(fresh=True)
        locator = current.with_entries(pairs)
        if locator is None and self._derived:
            locator = current  # a derived table is stored by the first write that has one in hand
        if locator is not None:
            self._write_locator(locator)

    def replace(self, rows: Mapping[str, Mapping[str, Any]]) -> None:
        """Make the cycle files hold exactly `rows` (migration, rebuild, a whole document)."""
        by_owner: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for section in _BULK_SECTIONS:
            for key, row in (rows.get(section) or {}).items():
                by_owner.setdefault(_bulk_owner(row), _bulk_empty_sections())[section][key] = row
        before = set(self.names())
        (self.dir / "cycles").mkdir(parents=True, exist_ok=True)
        for name, sections in sorted(by_owner.items()):
            self._write_shard(name, sections)
        _fsync_dir(self.dir / "cycles")
        self._write_locator(_BulkLocator.full(
            {name: [key for section in _BULK_SECTIONS for key in sections[section]]
             for name, sections in by_owner.items()}))
        for name in sorted(before - set(by_owner)):
            try:
                self._shard_path(name).unlink()
            except OSError:
                pass
            self._shards.pop(name, None)


class _BulkSection(abc.MutableMapping):
    """One moved section: rows in memory (`index.json`'s and this process's) over the cycle files.

    Lookups read only the cycle files `keys.bin` names for the key; iterating reads every
    cycle file (verification and rebuild only)."""

    def __init__(self, name: str, store: _BulkStore, inline: Mapping[str, Any],
                 overlay: Optional[Dict[str, Any]] = None):
        self.name = name
        self.store = store
        self.inline = inline
        self.overlay = overlay if overlay is not None else {}
        self._all: Optional[Dict[str, Any]] = None

    def copy_section(self) -> "_BulkSection":
        return _BulkSection(self.name, self.store, self.inline, dict(self.overlay))

    def _lookup(self, key: Any) -> Any:
        if self._all is not None:
            return self._all.get(key)
        stored = self.store.get(self.name, key) if _bulk_moved(self.name, key) and isinstance(key, str) else None
        found = _bulk_merge_row(self.name, stored, self.inline.get(key))
        return _bulk_merge_row(self.name, found, self.overlay.get(key))

    def get(self, key: Any, default: Any = None) -> Any:
        found = self._lookup(key)
        return default if found is None else found

    def __getitem__(self, key: Any) -> Any:
        found = self._lookup(key)
        if found is None:
            raise KeyError(key)
        return found

    def __contains__(self, key: Any) -> bool:
        return self._lookup(key) is not None

    def __setitem__(self, key: Any, value: Any) -> None:
        self.overlay[key] = value
        self._all = None

    def __delitem__(self, key: Any) -> None:
        raise TypeError("index {0} rows are never removed".format(self.name))

    def materialize(self) -> Dict[str, Any]:
        if self._all is None:
            rows = self.store.everything(self.name)
            for layer in (self.inline, self.overlay):
                for key, row in layer.items():
                    rows[key] = _bulk_merge_row(self.name, rows.get(key), row)
            self._all = rows
        return self._all

    def __iter__(self):
        return iter(self.materialize())

    def __len__(self) -> int:
        return len(self.materialize())

    def kept(self) -> Dict[str, Any]:
        """The rows that stay in `index.json`."""
        rows = {key: row for key, row in self.inline.items() if not _bulk_moved(self.name, key)}
        rows.update((key, row) for key, row in self.overlay.items() if not _bulk_moved(self.name, key))
        return rows


def _bulk_store(root: Path) -> Optional[_BulkStore]:
    """This root's cycle-file layout, or None while `index.json` still holds every row."""
    return _BulkStore(root) if (_bulk_dir(root) / "format.json").is_file() else None


def _with_bulk(document: artifact_index.IndexDocument, store: _BulkStore) -> artifact_index.IndexDocument:
    return replace(document, **{name: _BulkSection(name, store, getattr(document, name))
                                for name in _BULK_SECTIONS})


def attach_bulk_rows(root: Path, document: artifact_index.IndexDocument) -> artifact_index.IndexDocument:
    """Read-only: answer the moved sections of an `index.json` document from the cycle files too."""
    store = _bulk_store(root)
    return document if store is None else _with_bulk(document, store)


def _bulk_bound(index: artifact_index.IndexDocument, store: _BulkStore) -> bool:
    return all(isinstance(getattr(index, name), _BulkSection)
               and getattr(index, name).store.real == store.real for name in _BULK_SECTIONS)


def _bulk_all_rows(index: artifact_index.IndexDocument) -> Dict[str, Dict[str, Any]]:
    rows = {}
    for name in _BULK_SECTIONS:
        section = getattr(index, name)
        source = section.materialize() if isinstance(section, _BulkSection) else section
        rows[name] = {key: row for key, row in source.items() if _bulk_moved(name, key)}
    return rows


def _bulk_kept(index: artifact_index.IndexDocument) -> artifact_index.IndexDocument:
    stable = index.stable_ids
    kept = (stable.kept() if isinstance(stable, _BulkSection)
            else {key: row for key, row in stable.items() if not _bulk_moved("stable_ids", key)})
    return replace(index, stable_ids=kept, event_ids={}, streams={})


def _row_cycle(row: Any) -> Any:
    return row.get("cycle_id") if type(row) is dict else None


def _bulk_fold(index: artifact_index.IndexDocument, budget: int
               ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Split the moved rows held in memory into (rows to fold into cycle files, rows `index.json` keeps).

    Every row this write changed is folded, with every row `index.json` still holds for the same
    cycles; other cycles' rows follow whole, cycle by cycle, until `budget` rows.  One pass over
    the rows held in `index.json` (all of them while the layout is being turned on)."""
    sections = {name: getattr(index, name) for name in _BULK_SECTIONS}
    groups: Dict[Any, Dict[str, List[str]]] = {}
    stay: Dict[str, Dict[str, Any]] = {name: {} for name in _BULK_SECTIONS}
    for name, section in sections.items():
        kept_here = stay[name]
        for key, row in section.inline.items():
            if name == "stable_ids" and not (type(key) is str and key.startswith(_BULK_STABLE_PREFIXES)):
                kept_here[key] = row
                continue
            owner = row.get("cycle_id") if type(row) is dict else None
            members = groups.get(owner)
            if members is None:
                members = groups[owner] = {section_name: [] for section_name in _BULK_SECTIONS}
            members[name].append(key)
    fold = set()
    for name, section in sections.items():
        for key, row in section.overlay.items():
            if _bulk_moved(name, key):
                fold.add(_row_cycle(row))
                if key in section.inline:
                    fold.add(_row_cycle(section.inline[key]))
    size = {owner: sum(len(keys) for keys in members.values()) for owner, members in groups.items()}
    used = sum(size.get(owner, 0) for owner in fold)
    for owner in sorted(groups, key=str):
        if used >= budget:
            break
        if owner not in fold:
            fold.add(owner)
            used += size[owner]
    folded: Dict[str, Dict[str, Any]] = {name: {} for name in _BULK_SECTIONS}
    for owner, members in groups.items():
        target = folded if owner in fold else stay
        for name, keys in members.items():
            inline, rows = sections[name].inline, target[name]
            for key in keys:
                rows[key] = inline[key]
    for name, section in sections.items():
        for key, row in section.overlay.items():
            if _bulk_moved(name, key):
                folded[name][key] = _bulk_merge_row(name, folded[name].get(key), row)
            else:
                stay[name][key] = row
    return folded, stay


def _bulk_row_count(index: artifact_index.IndexDocument) -> int:
    return sum(len(getattr(index, name)) for name in _BULK_SECTIONS)


def _bulk_write(root: Path, index: artifact_index.IndexDocument, *, whole: bool = False
                ) -> Optional[Tuple[artifact_index.IndexDocument, artifact_index.IndexDocument]]:
    """Write the moved rows of `index`; return (what `index.json` keeps, the whole view after
    the write), or None while `index.json` is written whole.

    Cycle files and `keys.bin` are written before `index.json`.  Turning the layout on writes
    only `format.json`: every row is still in `index.json` then, and moves out a few cycles per
    write from this one on.  Only `whole` (rebuild) makes the cycle files hold exactly `index`;
    any other document whose sections are plain rows (one read before the layout was turned on,
    in the same locked section) is folded like the rows `index.json` holds."""
    store = _bulk_store(root)
    if store is None:
        if _bulk_row_count(index) < BULK_THRESHOLD_ROWS:
            return None
        store = _BulkStore(root)
        if isinstance(index.stable_ids, _BulkSection):
            index = replace(index, **{name: dict(getattr(index, name).materialize()) for name in _BULK_SECTIONS})
        (store.dir / "cycles").mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes(store.dir / "format.json", artifact_manifest.canonical_bytes(
            {"schema": 1, "sections": list(_BULK_SECTIONS),
             "stable_prefixes": list(_BULK_STABLE_PREFIXES)}))
        index = _with_bulk(index, store)
    elif not _bulk_bound(index, store):
        if whole or any(isinstance(getattr(index, name), _BulkSection) for name in _BULK_SECTIONS):
            store.replace(_bulk_all_rows(index))
            kept = _bulk_kept(index)
            return kept, _with_bulk(kept, store)
        index = _with_bulk(index, store)
    store = index.stable_ids.store
    folded, stay = _bulk_fold(index, BULK_FOLD_ROWS)
    store.add(folded)
    kept = replace(index, **stay)
    return kept, _with_bulk(kept, store)


def _write_index(root: Path, index: artifact_index.IndexDocument, *, whole: bool = False) -> None:
    """`whole` (rebuild only): the index is exactly `index`, including rows it no longer has."""
    global _INDEX_GENERATION
    split = _bulk_write(root, index, whole=whole)
    if split is not None:
        kept, index = split
        data = artifact_index.canonical_bytes(kept)
    else:
        data = artifact_index.canonical_bytes(index)
    key = _atomic_write_bytes(_index_path(root), data)
    if key is None:
        return
    _write_index_head(root, index, key)
    if holds_lock(root):
        with _INDEX_CACHE_LOCK:
            _INDEX_GENERATION = {"path": os.path.realpath(str(_index_path(root))),
                                 "key": key, "document": _copy_index_document(index),
                                 "hold_token": current_hold_token(),
                                 "thread": threading.get_ident()}


def _known_idempotency_keys(root: Path) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Recover caller-supplied idempotency keys from the current index.

    Returns ``(by_cycle_id, by_cycle_path)``.  The stable cycle id is the
    primary join: a campaign or cycle directory that was renamed (locator
    amendment, or a hand rename D-89 tolerates) still carries its
    ``cycle.cycle_id`` in the sealed manifest, so the key survives a rebuild
    and an exact retry stays a no-op.  Matching by path alone silently fell
    back to `manifest_id` after every rename (Project-C 2026-09-15, three
    `fallback_idempotency_keys`).  The path map remains for manifests whose
    cycle id is absent.
    """
    payload = _read_json(_index_path(root))
    if not isinstance(payload, dict):
        return {}, {}
    try:
        index = artifact_index.parse(payload)
    except Exception:
        return {}, {}
    cycle_paths = {
        cycle_id: row.get("cycle_path")
        for cycle_id, row in index.cycles.items()
        if isinstance(row, dict)
    }
    by_cycle: Dict[str, str] = {}
    by_path: Dict[str, str] = {}
    for key, row in index.manifests.items():
        if not isinstance(row, dict) or not isinstance(key, str):
            continue
        cycle_id = row.get("cycle_id")
        if isinstance(cycle_id, str):
            by_cycle[cycle_id] = key
        cycle_path = cycle_paths.get(cycle_id)
        if isinstance(cycle_path, str):
            by_path[cycle_path] = key
    return by_cycle, by_path


_CYCLE_RECORD_REL = Path(".runtime/artifact-producer/v1/cycles")
# The location `artifact_lifecycle.MANIFEST_SNAPSHOT_REL` names; that module
# imports this one, so the path is repeated here rather than imported back.
_MANIFEST_SNAPSHOT_REL = Path(".runtime/artifact-producer/v1/manifests")
_CYCLE_ID_FILE_RE = re.compile(r"^cyc_[0-9a-f]{32}$")


def _producer_cycle_ids(root: Path) -> frozenset:
    """Cycle IDs the root's producer has a record for (open, closed, or since deleted).

    A cycle's parent is a reference to one of these, not an ordering rule
    (§45 D-123), so a parent that is still open or already deleted is a known
    parent even when the index has no row for it."""
    try:
        names = os.listdir(str(Path(root) / _CYCLE_RECORD_REL))
    except OSError:
        return frozenset()
    return frozenset(name[:-5] for name in names
                     if name.endswith(".json") and _CYCLE_ID_FILE_RE.match(name[:-5]))


def _preserved_documents(root: Path, cycle_id: Any) -> List[Mapping[str, Any]]:
    """The cycle's earlier published documents (§45 D-124), read-only."""
    if not isinstance(cycle_id, str) or not artifact_identity.is_well_formed(cycle_id, "cycle"):
        return []
    try:
        directory = Path(root) / _MANIFEST_SNAPSHOT_REL / cycle_id
        names = sorted(os.listdir(str(directory)))
    except OSError:
        return []
    documents = []
    for name in names:
        path = directory / name
        if not name.endswith(".json") or path.is_symlink() or not path.is_file():
            continue
        try:
            document = json.loads(path.read_bytes().decode("utf-8"))
        except (OSError, ValueError, UnicodeError):
            continue
        if isinstance(document, dict) and document.get("manifest_revision_id") == name[:-5]:
            documents.append(document)
    return documents


def _retired_documents(root: Path) -> Dict[str, List[Mapping[str, Any]]]:
    """The preserved documents of every cycle the root deleted (§45 D-126), by cycle ID.

    A deleted cycle has no current row, but its IDs stay owned: a rebuilt index folds these
    documents in and then drops the cycle's own rows, so no ID a deleted cycle used is issued again."""
    directory = Path(root) / _CYCLE_RECORD_REL
    try:
        names = sorted(os.listdir(str(directory)))
    except OSError:
        return {}
    found: Dict[str, List[Mapping[str, Any]]] = {}
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            raw = (directory / name).read_bytes()
            if b'"deleted_at"' not in raw:
                continue
            record = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError, UnicodeError):
            continue
        if isinstance(record, dict) and record.get("deleted_at") and _CYCLE_ID_FILE_RE.match(str(record.get("cycle_id"))):
            documents = _preserved_documents(root, record["cycle_id"])
            if documents:
                found[record["cycle_id"]] = documents
    return found


def _compute_rebuilt_index(
    root: Path,
) -> Tuple[artifact_index.IndexDocument, List[str]]:
    root = Path(root)
    identity = ensure_root_identity(root)
    campaigns_dir = root / "campaigns"
    known_by_cycle, known_by_path = _known_idempotency_keys(root)
    fallback_keys: List[str] = []
    items = []
    preserved: Dict[str, List[Mapping[str, Any]]] = {}
    if campaigns_dir.exists():
        for campaign_dir in sorted(campaigns_dir.iterdir(), key=lambda path: path.name):
            if campaign_dir.name.startswith(".") or campaign_dir.is_symlink() or not campaign_dir.is_dir():
                continue
            for cycle_dir, _layout in artifact_locator.iter_cycle_dirs(campaign_dir):
                manifest_path = cycle_dir / "manifest.json"
                if not manifest_path.is_file():
                    continue
                document = json.loads(manifest_path.read_bytes().decode("utf-8"))
                earlier = [old for old in _preserved_documents(
                               root, (document.get("cycle") or {}).get("cycle_id")
                               if isinstance(document.get("cycle"), dict) else None)
                           if old.get("manifest_revision_id") != document.get("manifest_revision_id")]
                report = artifact_manifest.validate(document)
                if not report.ok and earlier:
                    # A document the runtime refreshed after the close names
                    # things only its earlier documents still declare.
                    report = artifact_manifest.validate_update(document, preserved=earlier, published=True)
                if not report.ok:
                    raise AdmissionRecoveryRequired(
                        "published manifest at {0} fails validation during rebuild".format(
                            manifest_path
                        )
                    )
                if document.get("repository_id") != identity.repository_id:
                    raise AdmissionRecoveryRequired(
                        "published manifest at {0} names a foreign repository_id".format(
                            manifest_path
                        )
                    )
                digest = artifact_manifest.manifest_digest(document)
                cycle_path = os.path.relpath(str(cycle_dir), str(root))
                # The idempotency key is caller-supplied and is deliberately NOT
                # part of D-6's closed manifest key set, so it cannot be derived
                # from the published folder. Recover it from the current index
                # when one exists -- that is what makes a rebuild byte-equal to
                # the incremental result (D-7 "index rebuild"). Only when the
                # index itself is gone do we fall back to the manifest id, and
                # that fallback is recorded as such rather than silently
                # producing a different document.
                cycle_row = document.get("cycle")
                cycle_id = cycle_row.get("cycle_id") if isinstance(cycle_row, dict) else None
                idempotency_key = known_by_cycle.get(cycle_id) if isinstance(cycle_id, str) else None
                if idempotency_key is None:
                    idempotency_key = known_by_path.get(cycle_path)
                if idempotency_key is None:
                    idempotency_key = document.get("manifest_id")
                    fallback_keys.append(idempotency_key)
                items.append((document, cycle_path, digest, idempotency_key))
                if earlier:
                    preserved[cycle_id] = earlier
    present = {(doc.get("cycle") or {}).get("cycle_id") for doc, _p, _d, _k in items}
    retired = {cycle_id: docs for cycle_id, docs in _retired_documents(root).items() if cycle_id not in present}
    if not items and not retired:
        return artifact_index.empty(identity.artifact_root_id), fallback_keys
    return artifact_index.build(
        items, known_parent_cycle_ids=_producer_cycle_ids(root), preserved=preserved,
        retired=retired, artifact_root_id=identity.artifact_root_id), fallback_keys


def rebuild_index(root: Path) -> artifact_index.IndexDocument:
    root = Path(root)
    try:
        # §45 D-126: a cycle or campaign folder moved, renamed or removed by hand is found first, so the
        # index is rebuilt from records that say where things are.  Never an error for the rebuild.
        import artifact_producer
        artifact_producer.reconcile_root(root)
    except Exception:  # noqa: BLE001
        pass
    index, fallback_keys = _compute_rebuilt_index(root)
    # A manifest-id fallback means the caller-supplied idempotency key was not
    # recoverable (index absent), so an exact retry with the original custom
    # key will no longer be a no-op. That divergence is a recorded, machine-
    # readable fact — written BEFORE the index is published so a crash between
    # the two writes cannot apply the fallback silently.
    report_path = _admission_dir(root) / "rebuild-report.json"
    _atomic_write_bytes(
        report_path,
        artifact_manifest.canonical_bytes(
            {
                "schema_version": 1,
                "fallback_idempotency_keys": sorted(
                    k for k in fallback_keys if isinstance(k, str)
                ),
            }
        ),
    )
    _write_index(root, index, whole=True)
    return index


def verify_index(root: Path) -> artifact_manifest.ValidationReport:
    root = Path(root)
    index_path = _index_path(root)
    rebuilt, _fallback_keys = _compute_rebuilt_index(root)
    rebuilt_bytes = artifact_index.canonical_bytes(rebuilt)
    store = _bulk_store(root)
    if store is not None:
        # The rows kept beside index.json are compared as one document with it.
        payload = _read_json(index_path)
        try:
            current_bytes = (artifact_index.canonical_bytes(_with_bulk(artifact_index.parse(payload), store))
                             if payload is not None else None)
        except ValueError:
            current_bytes = b""  # a hand-edited row that does not parse is drift too
        if current_bytes is not None and current_bytes != rebuilt_bytes:
            v = Violation("index-drift", "$", "current index bytes differ from rebuild")
            return artifact_manifest.ValidationReport(ok=False, violations=(v,))
        if store.uncovered():
            # Lookups follow keys.bin: a row it does not name would be taken for absent.
            v = Violation("index-drift", "$.index.bulk/keys.bin", "keys.bin does not name every cycle-file key")
            return artifact_manifest.ValidationReport(ok=False, violations=(v,))
        return artifact_manifest.ValidationReport(ok=True, violations=())
    try:
        current_size = index_path.stat().st_size
    except OSError:
        current_size = None
    current_raw = None
    if current_size is not None and current_size == len(rebuilt_bytes):
        try:
            current_raw = index_path.read_bytes()
        except OSError:
            current_raw = None
    if current_raw is not None and current_raw == rebuilt_bytes:
        pass  # the common canonical case: no second parse + re-serialization
    else:
        current_payload = _read_json(index_path)
        current_bytes = (
            artifact_manifest.canonical_bytes(current_payload) if current_payload is not None else None
        )
        if current_bytes is not None and current_bytes != rebuilt_bytes:
            v = Violation("index-drift", "$", "current index.json bytes differ from rebuild")
            return artifact_manifest.ValidationReport(ok=False, violations=(v,))
    stable_row_count = len(rebuilt.stable_ids) + len(rebuilt.event_ids)
    if stable_row_count >= _INDEX_ROW_WARN_COUNT or len(rebuilt_bytes) >= _INDEX_BYTE_WARN_SIZE:
        # Advisory only (W7E): the index still verifies; the size asks for a sharding review.
        v = Violation("index-size-warning", "$", "index row/byte count crossed the sharding-review threshold")
        return artifact_manifest.ValidationReport(ok=True, violations=(), warnings=(v,))
    return artifact_manifest.ValidationReport(ok=True, violations=())


# ---------------------------------------------------------------------------
# recover
# ---------------------------------------------------------------------------


def recover(
    root: Path,
    *,
    force: bool = False,
    now: Optional[float] = None,
    lock_timeout: float = LOCK_TIMEOUT_DEFAULT,
) -> Dict[str, Any]:
    """Public recovery entry: takes the same admission mutex as `admit()`.

    Without the mutex a direct `recover()` call could quarantine the staging
    tree of a live in-flight admission whose journal is still `preparing`.
    """
    root = Path(root)
    if not _admission_dir(root).exists():
        return {"rolled_forward": [], "rolled_back": []}
    lock_fd = _acquire_lock(root, lock_timeout, now=now)
    try:
        return _recover_locked(root, force=force, now=now)
    finally:
        _release_lock(root, lock_fd)


def _recover_locked(
    root: Path, *, force: bool = False, now: Optional[float] = None
) -> Dict[str, Any]:
    root = Path(root)
    admission_dir = _admission_dir(root)
    if not admission_dir.exists():
        return {"rolled_forward": [], "rolled_back": []}

    rolled_forward: List[str] = []
    rolled_back: List[str] = []

    for entry in _read_journals(root):
        idempotency_key = entry.get("idempotency_key")
        state = entry.get("state")
        if state == "committed":
            continue

        publish_target_rel = entry.get("publish_target")
        staging_rel = entry.get("staging_path")
        publish_target = root / publish_target_rel if publish_target_rel else None

        published_exists = publish_target is not None and publish_target.exists()

        if published_exists:
            manifest_path = publish_target / entry.get("cycle_relative", "") / "manifest.json"
            manifest_path = Path(os.path.normpath(str(manifest_path)))
            document = None
            if manifest_path.is_file():
                document = json.loads(manifest_path.read_bytes().decode("utf-8"))
            actual_digest = (
                artifact_manifest.manifest_digest(document) if document is not None else None
            )
            expected_digest = entry.get("manifest_digest")
            if document is not None and actual_digest == expected_digest:
                # D-8: roll-forward must verify the full declared payload, not
                # only manifest.json -- digests are the integrity evidence.
                payload_violations = _verify_staged_files(manifest_path.parent, document)
                if payload_violations:
                    if force:
                        _remove_journal(root, idempotency_key)
                        rolled_back.append(idempotency_key)
                        continue
                    codes = sorted({v.code for v in payload_violations})
                    raise AdmissionRecoveryRequired(
                        "published payload for {0} fails verification ({1})".format(
                            idempotency_key, ", ".join(codes)
                        )
                    )
                index = load_index(root)
                cycle_path = os.path.relpath(
                    str(manifest_path.parent), str(root)
                )
                index = artifact_index.apply(
                    index,
                    document,
                    cycle_path=cycle_path,
                    manifest_digest=actual_digest,
                    idempotency_key=idempotency_key,
                )
                _write_index(root, index)
                _write_journal(
                    root,
                    idempotency_key,
                    state="committed",
                    publish_target=publish_target_rel,
                    staging_path=staging_rel,
                    manifest_digest=expected_digest,
                    cycle_relative=entry.get("cycle_relative", ""),
                )
                rolled_forward.append(idempotency_key)
            elif force:
                _remove_journal(root, idempotency_key)
                rolled_back.append(idempotency_key)
            else:
                raise AdmissionRecoveryRequired(
                    "publish target {0} exists with a digest mismatch for {1}".format(
                        publish_target, idempotency_key
                    )
                )
        else:
            if staging_rel:
                staging_path = root / staging_rel
                if staging_path.exists():
                    _quarantine(root, staging_path, idempotency_key)
            _remove_journal(root, idempotency_key)
            rolled_back.append(idempotency_key)

    return {"rolled_forward": rolled_forward, "rolled_back": rolled_back}


# ---------------------------------------------------------------------------
# admit()
# ---------------------------------------------------------------------------


def admit(
    root: Path,
    request: AdmissionRequest,
    *,
    lock_timeout: float = LOCK_TIMEOUT_DEFAULT,
    now: Optional[float] = None,
) -> AdmissionOutcome:
    root = Path(root)

    # step 0 -- D-9: no durable output requested, zero filesystem access.
    if request.document is None and request.staging_source is None:
        return AdmissionOutcome(
            status="no-lineage",
            cycle_path=None,
            manifest_digest=None,
            violations=(),
            index_changed=False,
        )

    document = request.document
    if not isinstance(request.idempotency_key, str) or not _IDEMPOTENCY_KEY_RE.match(
        request.idempotency_key
    ):
        return AdmissionOutcome(
            status="rejected",
            cycle_path=None,
            manifest_digest=None,
            violations=(
                Violation("invalid-idempotency-key", "$", "idempotency_key must match [A-Za-z0-9._-]{1,128}"),
            ),
            index_changed=False,
        )

    # step 1 -- pure validation, zero filesystem access.
    report = artifact_manifest.validate(document)
    if not report.ok:
        return AdmissionOutcome(
            status="rejected",
            cycle_path=None,
            manifest_digest=None,
            violations=report.violations,
            index_changed=False,
        )

    digest = artifact_manifest.manifest_digest(document)

    # step 2 -- global mutex.
    lock_fd = _acquire_lock(root, lock_timeout, now=now)
    try:
        # step 3 -- recover previous attempts (already under the mutex).
        _recover_locked(root, now=now)

        # step 4 -- root identity bootstrap.
        identity = ensure_root_identity(root, allocator=request.allocator, now=now)

        # step 4b -- repository tenancy is checked before every other verdict,
        # including the idempotent no-op: a forged index row must never let a
        # foreign repository's manifest through (D-4).
        if document.get("repository_id") != identity.repository_id:
            return AdmissionOutcome(
                status="rejected",
                cycle_path=None,
                manifest_digest=digest,
                violations=(
                    Violation(
                        "index-repository-identity-mismatch",
                        "$.repository_id",
                        "manifest repository_id does not match frozen root identity",
                    ),
                ),
                index_changed=False,
            )

        # step 5 -- load index.
        index = load_index(root)

        # step 6 -- idempotent no-op check.
        if artifact_index.idempotent_match(
            index, document, idempotency_key=request.idempotency_key, manifest_digest=digest
        ):
            existing_cycle = index.manifests[request.idempotency_key].get("cycle_id")
            cycle_path = index.cycles.get(existing_cycle, {}).get("cycle_path")
            # The index is a derived accelerator: a no-op verdict must be
            # backed by the canonical folder it claims exists, or a forged or
            # drifted index row would fake an admission that never published.
            published_manifest = None
            if isinstance(cycle_path, str) and not Path(cycle_path).is_absolute():
                try:
                    published_manifest = _path_under_root(
                        root, root / cycle_path / "manifest.json"
                    )
                except ValueError:
                    published_manifest = None
            if published_manifest is None or not published_manifest.is_file():
                return AdmissionOutcome(
                    status="rejected",
                    cycle_path=None,
                    manifest_digest=digest,
                    violations=(
                        Violation(
                            "index-idempotent-target-missing",
                            "$",
                            "index claims this idempotency key was admitted but the "
                            "canonical cycle folder is missing; run verify_index()/rebuild_index()",
                        ),
                    ),
                    index_changed=False,
                )
            return AdmissionOutcome(
                status="noop-idempotent",
                cycle_path=cycle_path,
                manifest_digest=digest,
                violations=(),
                index_changed=False,
            )

        # step 7 -- index-level checks, canonical untouched so far.
        index_report = artifact_index.check(
            index,
            document,
            idempotency_key=request.idempotency_key,
            manifest_digest=digest,
            repository_id=identity.repository_id,
            known_parent_cycle_ids=_producer_cycle_ids(root),
        )
        if not index_report.ok:
            return AdmissionOutcome(
                status="rejected",
                cycle_path=None,
                manifest_digest=digest,
                violations=index_report.violations,
                index_changed=False,
            )

        cycle = document["cycle"]
        campaign_id = document["campaign"]["campaign_id"]
        cycle_id = cycle["cycle_id"]

        # step 8 -- publish plan.
        try:
            publish_target, deepest_parent, staging = publish_plan(root, campaign_id, cycle_id)
        except ValueError as exc:
            return AdmissionOutcome(
                status="rejected",
                cycle_path=None,
                manifest_digest=digest,
                violations=(Violation("publish-path-invalid", "$", str(exc)),),
                index_changed=False,
            )
        if staging is None:
            # cycle_id already exists on disk and it was not an idempotent
            # retry (index.check would have already rejected it, but guard
            # defensively for out-of-band index/fs drift).
            return AdmissionOutcome(
                status="rejected",
                cycle_path=None,
                manifest_digest=digest,
                violations=(
                    Violation("index-cycle-id-duplicate", "$.cycle.cycle_id", "cycle folder already exists"),
                ),
                index_changed=False,
            )

        # step 9 -- same-device staging sibling.
        if os.stat(str(deepest_parent)).st_dev != os.stat(str(root)).st_dev:
            return AdmissionOutcome(
                status="rejected",
                cycle_path=None,
                manifest_digest=digest,
                violations=(Violation("staging-cross-device", "$", "staging sibling is not on the same device as root"),),
                index_changed=False,
            )

        try:
            cycles_dir_for_cycle = _path_under_root(
                root, root / "campaigns" / campaign_id / "cycles"
            )
            cycle_dir = _path_under_root(root, cycles_dir_for_cycle / cycle_id)
        except ValueError as exc:
            _remove_tree(staging)
            return AdmissionOutcome(
                status="rejected",
                cycle_path=None,
                manifest_digest=digest,
                violations=(Violation("publish-path-invalid", "$", str(exc)),),
                index_changed=False,
            )

        try:
            cycle_content_dir = _stage_tree(
                staging, publish_target, cycle_dir, document, request.staging_source
            )
        except OSError as exc:
            _remove_tree(staging)
            return AdmissionOutcome(
                status="rejected",
                cycle_path=None,
                manifest_digest=digest,
                violations=(Violation("staging-write-failed", "$", str(exc)),),
                index_changed=False,
            )

        # step 11 -- verify staged files.
        stage_violations = list(
            _verify_staging_source(request.staging_source, document)
        ) + list(_verify_staged_files(cycle_content_dir, document))
        if stage_violations:
            _remove_tree(staging)
            return AdmissionOutcome(
                status="rejected",
                cycle_path=None,
                manifest_digest=digest,
                violations=tuple(
                    sorted(stage_violations, key=lambda v: (v.code, v.path, v.detail))
                ),
                index_changed=False,
            )

        # Computed from the two *canonical* future paths, not from the
        # staging path -- the staging directory's own name (`.admitting-…`)
        # must never leak into the journal's cycle_relative record.
        cycle_relative = os.path.relpath(str(cycle_dir), str(publish_target))
        publish_target_rel = os.path.relpath(str(publish_target), str(root))
        staging_rel = os.path.relpath(str(staging), str(root))

        # step 12 -- journal: preparing.
        _write_journal(
            root,
            request.idempotency_key,
            state="preparing",
            publish_target=publish_target_rel,
            staging_path=staging_rel,
            manifest_digest=digest,
            cycle_relative=("" if cycle_relative == "." else cycle_relative),
        )

        # step 13 -- confirm publish target absent (TOCTOU window closed by
        # the global mutex held since step 2).
        try:
            os.lstat(str(publish_target))
            already_present = True
        except FileNotFoundError:
            already_present = False

        if already_present:
            _remove_tree(staging)
            _remove_journal(root, request.idempotency_key)
            return AdmissionOutcome(
                status="rejected",
                cycle_path=None,
                manifest_digest=digest,
                violations=(
                    Violation(
                        "publish-target-already-exists",
                        "$",
                        "canonical publish target appeared during admission",
                    ),
                ),
                index_changed=False,
            )

        # step 14 -- SINGLE COMMIT POINT.
        atomic_publish.rename_directory_locked(staging, publish_target)

        # After the commit point a failure can no longer mean "nothing was
        # admitted": the canonical folder is visible and the journal (still
        # `preparing`) plus `recover()` roll the admission forward. Surface
        # that state as a typed recovery signal instead of an anonymous raise
        # that looks like a zero-commit rejection.
        try:
            # step 15.
            _fsync_dir(publish_target.parent)

            # step 16.
            _write_journal(
                root,
                request.idempotency_key,
                state="published",
                publish_target=publish_target_rel,
                staging_path=staging_rel,
                manifest_digest=digest,
                cycle_relative=("" if cycle_relative == "." else cycle_relative),
            )

            # step 17.
            cycle_path = os.path.relpath(str(cycle_dir), str(root))
            index = artifact_index.apply(
                index,
                document,
                cycle_path=cycle_path,
                manifest_digest=digest,
                idempotency_key=request.idempotency_key,
            )
            _write_index(root, index)

            # step 18.
            _write_journal(
                root,
                request.idempotency_key,
                state="committed",
                publish_target=publish_target_rel,
                staging_path=staging_rel,
                manifest_digest=digest,
                cycle_relative=("" if cycle_relative == "." else cycle_relative),
            )
        except AdmissionRecoveryRequired:
            raise
        except BaseException as exc:
            raise AdmissionRecoveryRequired(
                "cycle {0} was published but the post-publish journal/index "
                "update failed; run recover() to roll it forward".format(
                    request.idempotency_key
                )
            ) from exc

        return AdmissionOutcome(
            status="admitted",
            cycle_path=cycle_path,
            manifest_digest=digest,
            violations=(),
            index_changed=True,
        )
    finally:
        # step 19.
        _release_lock(root, lock_fd)
