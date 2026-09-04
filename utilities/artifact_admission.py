from __future__ import annotations

"""D-8/D-9 atomic folder admission.

Effect layer: this module is the only place in rollout step 1 that touches
the filesystem for producer admission. Exclusivity comes from an OS advisory
lock (`flock`) held for the whole admission — process death releases it, so
there is no dead-holder reclamation to race on — never from a rename flag
(F-1/F-2 measured this root does not give no-replace rename). The single
commit point is the canonical publish `os.rename(staging, publish_target)`
— see `admit()` step 14.

`.runtime/artifact-admission/v1/index.json` is a derived, rebuildable
accelerator; the durable source of truth is the published manifest and its
events. `.runtime/artifact-admission/v1/root-identity.json` is the only value
here that cannot be rebuilt from published manifests.
"""

import errno
import fcntl
import json
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import artifact_index
import artifact_locator
import artifact_manifest
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
    "- `index.json` can always be regenerated: `artifact_admission.rebuild_index()`.\n"
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


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(".{0}.{1}.tmp".format(path.name, _new_token()))
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(temp), str(path))
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass
    _fsync_dir(path.parent)


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
        for path_value, _digest, _size, _media in artifact_manifest.declared_locators(document):
            if not isinstance(path_value, str):
                continue
            src = Path(staging_source) / path_value
            dst = cycle_content_dir / path_value
            dst.parent.mkdir(parents=True, exist_ok=True)
            if os.path.islink(str(src)) or not os.path.isfile(str(src)):
                continue  # surfaced as a violation by _verify_staged_files
            with open(str(src), "rb") as rfh, open(str(dst), "wb") as wfh:
                wfh.write(rfh.read())
                wfh.flush()
                # Payload bytes must be durable before the publish rename makes
                # the folder canonical; a crash must not leave a truncated file
                # behind a matching journal digest.
                os.fsync(wfh.fileno())

    for dirpath, _dirnames, _filenames in os.walk(str(staging)):
        _fsync_dir(Path(dirpath))

    return cycle_content_dir


def _walk_all_entries(top: Path):
    for dirpath, dirnames, filenames in os.walk(str(top)):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            yield Path(full)


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
        if rel != "manifest.json" and rel not in declared_paths:
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
            actual_digest = artifact_manifest.digest_bytes(full.read_bytes())
            if actual_digest != digest:
                violations.append(
                    Violation(
                        "staging-digest-mismatch",
                        path_value,
                        "expected {0}, got {1}".format(digest, actual_digest),
                    )
                )

    all_files = set()
    for entry in _walk_all_entries(cycle_content_dir):
        if entry.is_file() and not os.path.islink(str(entry)):
            rel = os.path.relpath(str(entry), str(cycle_content_dir))
            rel = rel.replace(os.sep, "/")
            if rel == "manifest.json":
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
            os.rename(str(staging), str(dest))
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


def load_index(root: Path) -> artifact_index.IndexDocument:
    root = Path(root)
    path = _index_path(root)
    payload = _read_json(path)
    if payload is None:
        identity = ensure_root_identity(root)
        return artifact_index.empty(identity.artifact_root_id)
    return artifact_index.parse(payload)


def _write_index(root: Path, index: artifact_index.IndexDocument) -> None:
    _atomic_write_bytes(_index_path(root), artifact_index.canonical_bytes(index))


def _known_idempotency_keys(root: Path) -> Dict[str, str]:
    """Map `cycle_path -> idempotency_key` recovered from the current index."""
    payload = _read_json(_index_path(root))
    if not isinstance(payload, dict):
        return {}
    try:
        index = artifact_index.parse(payload)
    except Exception:
        return {}
    cycle_paths = {
        cycle_id: row.get("cycle_path")
        for cycle_id, row in index.cycles.items()
        if isinstance(row, dict)
    }
    keys = {}
    for key, row in index.manifests.items():
        if not isinstance(row, dict):
            continue
        cycle_path = cycle_paths.get(row.get("cycle_id"))
        if isinstance(cycle_path, str) and isinstance(key, str):
            keys[cycle_path] = key
    return keys


def _compute_rebuilt_index(
    root: Path,
) -> Tuple[artifact_index.IndexDocument, List[str]]:
    root = Path(root)
    identity = ensure_root_identity(root)
    campaigns_dir = root / "campaigns"
    known_keys = _known_idempotency_keys(root)
    fallback_keys: List[str] = []
    items = []
    if campaigns_dir.exists():
        for campaign_dir in sorted(campaigns_dir.iterdir(), key=lambda path: path.name):
            if campaign_dir.name.startswith(".") or campaign_dir.is_symlink() or not campaign_dir.is_dir():
                continue
            for cycle_dir, _layout in artifact_locator.iter_cycle_dirs(campaign_dir):
                manifest_path = cycle_dir / "manifest.json"
                if not manifest_path.is_file():
                    continue
                document = json.loads(manifest_path.read_bytes().decode("utf-8"))
                report = artifact_manifest.validate(document)
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
                idempotency_key = known_keys.get(cycle_path)
                if idempotency_key is None:
                    idempotency_key = document.get("manifest_id")
                    fallback_keys.append(idempotency_key)
                items.append((document, cycle_path, digest, idempotency_key))
    if not items:
        return artifact_index.empty(identity.artifact_root_id), fallback_keys
    return artifact_index.build(items), fallback_keys


def rebuild_index(root: Path) -> artifact_index.IndexDocument:
    root = Path(root)
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
    _write_index(root, index)
    return index


def verify_index(root: Path) -> artifact_manifest.ValidationReport:
    root = Path(root)
    current_payload = _read_json(_index_path(root))
    current_bytes = (
        artifact_manifest.canonical_bytes(current_payload) if current_payload is not None else None
    )
    rebuilt, _fallback_keys = _compute_rebuilt_index(root)
    rebuilt_bytes = artifact_index.canonical_bytes(rebuilt)
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
        os.rename(str(staging), str(publish_target))

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
