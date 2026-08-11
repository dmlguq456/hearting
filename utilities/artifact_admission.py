from __future__ import annotations

"""D-8/D-9 atomic folder admission.

Effect layer: this module is the only place in rollout step 1 that touches
the filesystem for producer admission. Exclusivity comes from an EEXIST-class
`mkdir` mutex (F-3), never from a rename flag (F-1/F-2 measured this root does
not give no-replace rename). The single commit point is the canonical publish
`os.rename(staging, publish_target)` — see `admit()` step 14.

`.runtime/artifact-admission/v1/index.json` is a derived, rebuildable
accelerator; the durable source of truth is the published manifest and its
events. `.runtime/artifact-admission/v1/root-identity.json` is the only value
here that cannot be rebuilt from published manifests.
"""

import errno
import json
import os
import re
import socket
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import artifact_index
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


def _boot_id() -> Optional[str]:
    try:
        with open("/proc/sys/kernel/random/boot_id", "r") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _proc_start_ticks(pid: int) -> Optional[int]:
    try:
        with open("/proc/{0}/stat".format(pid), "r") as fh:
            raw = fh.read()
    except OSError:
        return None
    # field 2 is "(comm)" which may itself contain spaces/parens; split on the
    # last ')' to skip past it safely, then index from there.
    close_paren = raw.rfind(")")
    if close_paren == -1:
        return None
    rest = raw[close_paren + 1 :].split()
    # rest[0] is field 3 (state); field 22 (starttime) is rest[22 - 3] = rest[19]
    idx = 22 - 3
    if len(rest) <= idx:
        return None
    try:
        return int(rest[idx])
    except ValueError:
        return None


def _holder_payload(attempt: str) -> Dict[str, Any]:
    pid = os.getpid()
    return {
        "pid": pid,
        "host": socket.gethostname(),
        "proc_start_ticks": _proc_start_ticks(pid),
        "acquired_at": time.time(),
        "attempt": attempt,
        "boot_id": _boot_id(),
    }


def _pid_is_live(payload: Dict[str, Any]) -> bool:
    if payload.get("host") != socket.gethostname():
        # Cross-host liveness is out of scope this cycle -- assume alive,
        # never hijack.
        return True
    current_boot_id = _boot_id()
    recorded_boot_id = payload.get("boot_id")
    if current_boot_id is not None and recorded_boot_id is not None:
        if current_boot_id != recorded_boot_id:
            return False  # proven reboot since lock was taken
    pid = payload.get("pid")
    if not isinstance(pid, int):
        return False
    current_ticks = _proc_start_ticks(pid)
    if current_ticks is None:
        return False  # /proc/<pid> absent
    recorded_ticks = payload.get("proc_start_ticks")
    if recorded_ticks is None:
        return True  # cannot disprove liveness; do not hijack
    return current_ticks == recorded_ticks


def _lock_dir(root: Path) -> Path:
    return _admission_dir(root) / "lock"


def _acquire_lock(root: Path, timeout: float, now: Optional[float] = None) -> None:
    lock_dir = _lock_dir(root)
    holder_path = lock_dir / "holder.json"
    deadline = (now if now is not None else time.time()) + timeout
    backoff = 0.01
    attempt_token = _new_token()
    while True:
        lock_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.mkdir(str(lock_dir))
        except FileExistsError:
            holder = _read_json(holder_path)
            if holder is None:
                # race: lock dir exists but holder not written yet, or a dead
                # holder already reclaimed; brief retry.
                time.sleep(min(backoff, 0.05))
                backoff = min(backoff * 2, 0.5)
                if time.time() > deadline:
                    raise AdmissionBusy("admission lock busy (no holder record)")
                continue
            if not _pid_is_live(holder):
                _reclaim_dead_lock(lock_dir, holder_path)
                continue
            if time.time() > deadline:
                raise AdmissionBusy("admission lock held by live process {0!r}".format(holder))
            time.sleep(min(backoff, 0.5))
            backoff = min(backoff * 2, 0.5)
            continue
        else:
            created = _create_exclusive_json(holder_path, _holder_payload(attempt_token))
            if not created:
                # extremely unlikely race; treat as busy and retry loop
                try:
                    os.rmdir(str(lock_dir))
                except OSError:
                    pass
                continue
            return


def _reclaim_dead_lock(lock_dir: Path, holder_path: Path) -> None:
    try:
        holder_path.unlink()
    except OSError:
        pass
    try:
        os.rmdir(str(lock_dir))
    except OSError:
        pass


def _release_lock(root: Path) -> None:
    lock_dir = _lock_dir(root)
    holder_path = lock_dir / "holder.json"
    try:
        holder_path.unlink()
    except OSError:
        pass
    try:
        os.rmdir(str(lock_dir))
    except OSError:
        pass


def force_release_lock(root: Path) -> None:
    """Operator escape hatch. Not called automatically."""
    _release_lock(root)


# ---------------------------------------------------------------------------
# publish plan
# ---------------------------------------------------------------------------


def publish_plan(root: Path, campaign_id: str, cycle_id: str) -> Tuple[Path, Path, Optional[Path]]:
    root = Path(root)
    campaigns_dir = root / "campaigns"
    campaign_dir = campaigns_dir / campaign_id
    cycles_dir = campaign_dir / "cycles"
    cycle_dir = cycles_dir / cycle_id

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


def _compute_rebuilt_index(root: Path) -> artifact_index.IndexDocument:
    root = Path(root)
    identity = ensure_root_identity(root)
    campaigns_dir = root / "campaigns"
    known_keys = _known_idempotency_keys(root)
    items = []
    if campaigns_dir.exists():
        for campaign_id in sorted(os.listdir(str(campaigns_dir))):
            if campaign_id.startswith("."):
                continue
            cycles_dir = campaigns_dir / campaign_id / "cycles"
            if not cycles_dir.is_dir():
                continue
            for cycle_id in sorted(os.listdir(str(cycles_dir))):
                if cycle_id.startswith("."):
                    continue
                cycle_dir = cycles_dir / cycle_id
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
                idempotency_key = known_keys.get(cycle_path) or document.get("manifest_id")
                items.append((document, cycle_path, digest, idempotency_key))
    if not items:
        return artifact_index.empty(identity.artifact_root_id)
    return artifact_index.build(items)


def rebuild_index(root: Path) -> artifact_index.IndexDocument:
    root = Path(root)
    index = _compute_rebuilt_index(root)
    _write_index(root, index)
    return index


def verify_index(root: Path) -> artifact_manifest.ValidationReport:
    root = Path(root)
    current_payload = _read_json(_index_path(root))
    current_bytes = (
        artifact_manifest.canonical_bytes(current_payload) if current_payload is not None else None
    )
    rebuilt = _compute_rebuilt_index(root)
    rebuilt_bytes = artifact_index.canonical_bytes(rebuilt)
    if current_bytes is not None and current_bytes != rebuilt_bytes:
        v = Violation("index-drift", "$", "current index.json bytes differ from rebuild")
        return artifact_manifest.ValidationReport(ok=False, violations=(v,))
    stable_row_count = len(rebuilt.stable_ids) + len(rebuilt.event_ids)
    if stable_row_count >= _INDEX_ROW_WARN_COUNT or len(rebuilt_bytes) >= _INDEX_BYTE_WARN_SIZE:
        v = Violation("index-size-warning", "$", "index row/byte count crossed the sharding-review threshold")
        return artifact_manifest.ValidationReport(ok=False, violations=(v,))
    return artifact_manifest.ValidationReport(ok=True, violations=())


# ---------------------------------------------------------------------------
# recover
# ---------------------------------------------------------------------------


def recover(root: Path, *, force: bool = False, now: Optional[float] = None) -> Dict[str, Any]:
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
    _acquire_lock(root, lock_timeout, now=now)
    try:
        # step 3 -- recover previous attempts.
        recover(root, now=now)

        # step 4 -- root identity bootstrap.
        identity = ensure_root_identity(root, allocator=request.allocator, now=now)

        # step 5 -- load index.
        index = load_index(root)

        # step 6 -- idempotent no-op check.
        if artifact_index.idempotent_match(
            index, document, idempotency_key=request.idempotency_key, manifest_digest=digest
        ):
            existing_cycle = index.manifests[request.idempotency_key].get("cycle_id")
            cycle_path = index.cycles.get(existing_cycle, {}).get("cycle_path")
            return AdmissionOutcome(
                status="noop-idempotent",
                cycle_path=cycle_path,
                manifest_digest=digest,
                violations=(),
                index_changed=False,
            )

        # step 7 -- index-level checks, canonical untouched so far.
        index_report = artifact_index.check(
            index, document, idempotency_key=request.idempotency_key, manifest_digest=digest
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
        publish_target, deepest_parent, staging = publish_plan(root, campaign_id, cycle_id)
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

        cycles_dir_for_cycle = root / "campaigns" / campaign_id / "cycles"
        cycle_dir = cycles_dir_for_cycle / cycle_id

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

        return AdmissionOutcome(
            status="admitted",
            cycle_path=cycle_path,
            manifest_digest=digest,
            violations=(),
            index_changed=True,
        )
    finally:
        # step 19.
        _release_lock(root)
