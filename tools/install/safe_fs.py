"""Operation-scoped filesystem authority and successor-preserving rollback.

This module is deliberately stdlib-only.  It centralizes the rules that make a
destructive installer operation legitimate:

* canonical containment or an exact closed allowlist;
* an explicit ownership proof;
* an exact current-state comparison immediately before mutation; and
* a stable lock keyed by the canonical target, never by a runtime home.

Callers may keep file payloads in an in-memory :class:`Transaction` for
rollback.  Payloads are never included in the public state representation, so
profile or credential bytes cannot leak into journals or reports.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field, replace
import hashlib
import os
from pathlib import Path
import shutil
import stat
import tempfile
from typing import Iterable, Mapping, Sequence

try:  # POSIX is the primary installer runtime.
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows only.
    fcntl = None

try:  # The installer also has import-only coverage on Windows.
    import pwd
except ImportError:  # pragma: no cover - exercised on Windows only.
    pwd = None


STATE_PAYLOAD_LIMIT = 8 * 1024 * 1024


class SafetyError(RuntimeError):
    """A typed refusal to mutate an unproved or concurrently changed path."""

    def __init__(self, code: str, path: Path | str, detail: str = "") -> None:
        self.code = code
        self.path = str(path)
        self.detail = detail
        message = f"{code}: {self.path}"
        if detail:
            message += f": {detail}"
        super().__init__(message)


@dataclass(frozen=True)
class PathState:
    """Exact leaf identity.

    ``payload`` is optional in-memory rollback material and is excluded from
    equality and representation.  Equality therefore remains safe to print.
    """

    kind: str
    device: int | None = None
    inode: int | None = None
    mode: int | None = None
    size: int | None = None
    digest: str | None = None
    link_target: str | None = None
    payload: bytes | None = field(default=None, compare=False, repr=False)

    def public(self) -> dict[str, object | None]:
        return {
            "kind": self.kind,
            "device": self.device,
            "inode": self.inode,
            "mode": self.mode,
            "size": self.size,
            "digest": self.digest,
            "link_target": self.link_target,
        }

    @classmethod
    def from_public(cls, value: object) -> "PathState":
        if not isinstance(value, dict):
            raise SafetyError("ownership-unproved", "<state>", "state is not an object")
        expected_keys = {
            "kind", "device", "inode", "mode", "size", "digest", "link_target"
        }
        if set(value) != expected_keys or value.get("kind") not in {
            "missing", "file", "symlink", "directory", "other"
        }:
            raise SafetyError("ownership-unproved", "<state>", "state shape is invalid")
        numeric = {"device", "inode", "mode", "size"}
        for key in numeric:
            item = value.get(key)
            if item is not None and (not isinstance(item, int) or isinstance(item, bool)):
                raise SafetyError("ownership-unproved", "<state>", f"{key} is invalid")
        for key in ("digest", "link_target"):
            item = value.get(key)
            if item is not None and not isinstance(item, str):
                raise SafetyError("ownership-unproved", "<state>", f"{key} is invalid")
        return cls(
            kind=str(value["kind"]),
            device=value.get("device"),
            inode=value.get("inode"),
            mode=value.get("mode"),
            size=value.get("size"),
            digest=value.get("digest"),
            link_target=value.get("link_target"),
        )


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _tree_digest(path: Path, *, exclude_names: Sequence[str] = ()) -> str:
    """Hash one real directory tree without following any symlink."""

    digest = hashlib.sha256()

    excluded = frozenset(exclude_names)

    def visit(directory: Path, *, root: bool = False) -> None:
        before = os.lstat(directory)
        if not stat.S_ISDIR(before.st_mode):
            raise SafetyError("expected-state-mismatch", directory, "directory kind changed")
        digest.update(b"D\0" + str(stat.S_IMODE(before.st_mode)).encode() + b"\0")
        with os.scandir(directory) as entries:
            ordered = sorted(entries, key=lambda item: os.fsencode(item.name))
        for entry in ordered:
            if root and entry.name in excluded:
                continue
            child = directory / entry.name
            info = os.lstat(child)
            digest.update(os.fsencode(entry.name) + b"\0")
            if stat.S_ISLNK(info.st_mode):
                digest.update(b"L\0" + os.fsencode(os.readlink(child)) + b"\0")
            elif stat.S_ISREG(info.st_mode):
                payload = child.read_bytes()
                after = os.lstat(child)
                if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    raise SafetyError(
                        "expected-state-mismatch", child, "file changed while hashing"
                    )
                digest.update(
                    b"F\0"
                    + str(stat.S_IMODE(info.st_mode)).encode()
                    + b"\0"
                    + hashlib.sha256(payload).digest()
                )
            elif stat.S_ISDIR(info.st_mode):
                visit(child)
            else:
                raise SafetyError(
                    "ownership-unproved", child, "special entries cannot be tree-owned"
                )
        after = os.lstat(directory)
        if (before.st_dev, before.st_ino, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_mtime_ns,
        ):
            raise SafetyError(
                "expected-state-mismatch", directory, "directory changed while hashing"
            )

    visit(path, root=True)
    return digest.hexdigest()


def capture_state(
    path: Path | str,
    *,
    include_payload: bool = False,
    payload_limit: int = STATE_PAYLOAD_LIMIT,
    exclude_names: Sequence[str] = (),
) -> PathState:
    """Capture a leaf with no symlink following and a stable content check."""

    leaf = Path(path)
    try:
        before = os.lstat(leaf)
    except FileNotFoundError:
        return PathState("missing")

    common = {
        "device": before.st_dev,
        "inode": before.st_ino,
        "mode": stat.S_IMODE(before.st_mode),
        "size": before.st_size,
    }
    if stat.S_ISLNK(before.st_mode):
        target = os.readlink(leaf)
        after = os.lstat(leaf)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise SafetyError("expected-state-mismatch", leaf, "symlink changed while reading")
        return PathState("symlink", link_target=target, **common)
    if stat.S_ISREG(before.st_mode):
        if include_payload and before.st_size > payload_limit:
            raise SafetyError(
                "ownership-unproved", leaf, f"rollback payload exceeds {payload_limit} bytes"
            )
        payload = leaf.read_bytes()
        after = os.lstat(leaf)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise SafetyError("expected-state-mismatch", leaf, "file changed while reading")
        return PathState(
            "file",
            digest=_sha256_bytes(payload),
            payload=payload if include_payload else None,
            **common,
        )
    if stat.S_ISDIR(before.st_mode):
        return PathState(
            "directory",
            digest=_tree_digest(leaf, exclude_names=exclude_names),
            **common,
        )
    return PathState("other", **common)


def _account_homes() -> set[Path]:
    homes: set[Path] = set()
    raw = os.environ.get("HOME")
    if raw:
        homes.add(Path(os.path.abspath(os.path.expanduser(raw))))
    if pwd is not None and hasattr(os, "geteuid"):
        try:
            homes.add(Path(os.path.abspath(pwd.getpwuid(os.geteuid()).pw_dir)))
        except (KeyError, OSError):  # pragma: no cover - unusual NSS failure.
            pass
    return homes


def _canonical_leaf(path: Path | str, *, reject_leaf_symlink: bool) -> Path:
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        raise SafetyError("unsafe-ambient-path", raw, "target must be absolute")
    normalized = Path(os.path.abspath(os.fspath(raw)))
    parts = normalized.parts
    current = Path(parts[0])
    for index, part in enumerate(parts[1:], start=1):
        current = current / part
        is_leaf = index == len(parts) - 1
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) and (not is_leaf or reject_leaf_symlink):
            code = "unsafe-ambient-path" if not is_leaf else "ownership-unproved"
            raise SafetyError(code, current, "symlink is outside the declared leaf policy")
        if not is_leaf and not stat.S_ISDIR(info.st_mode):
            raise SafetyError("unsafe-ambient-path", current, "parent is not a directory")
    return normalized


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _assert_fixture_boundary(path: Path) -> None:
    raw = os.environ.get("HEARTING_FIXTURE_ROOT")
    if not raw:
        return
    fixture = _canonical_leaf(Path(raw).expanduser().absolute(), reject_leaf_symlink=True)
    if path != fixture and not _is_within(path, fixture):
        raise SafetyError(
            "target-outside-fixture", path, f"fixture_root={fixture}"
        )


@dataclass(frozen=True)
class Authority:
    """Closed mutation authority for one canonical leaf."""

    target: Path
    owner: str
    allowed_roots: tuple[Path, ...]
    allowed_paths: frozenset[Path]
    allow_leaf_symlink: bool = True
    expected: PathState | None = None

    def with_expected(self, expected: PathState) -> "Authority":
        return replace(self, expected=expected)


def authority(
    path: Path | str,
    *,
    owner: str,
    allowed_roots: Sequence[Path | str] = (),
    allowed_paths: Sequence[Path | str] = (),
    allow_leaf_symlink: bool = True,
    expected: PathState | None = None,
) -> Authority:
    """Validate and construct a non-wildcard mutation authority.

    This function is mutation-free.  In particular, it creates neither parent
    directories nor lock files, which lets callers prove invalid-before-mutation.
    """

    if not owner or not owner.strip():
        raise SafetyError("ownership-unproved", path, "owner proof is empty")
    target = _canonical_leaf(path, reject_leaf_symlink=not allow_leaf_symlink)
    _assert_fixture_boundary(target)
    roots = tuple(
        _canonical_leaf(root, reject_leaf_symlink=True) for root in allowed_roots
    )
    exact = frozenset(
        _canonical_leaf(item, reject_leaf_symlink=False) for item in allowed_paths
    )
    root_path = Path(target.anchor)
    protected = {root_path, *_account_homes()}
    for item in list(protected):
        protected.update(item.parents)
    if target in protected:
        raise SafetyError("unsafe-ambient-path", target, "root, HOME, and their ancestors are protected")
    if target not in exact and not any(_is_within(target, root) and target != root for root in roots):
        raise SafetyError(
            "ownership-unproved", target, "target is outside the exact allowlist/owned roots"
        )
    return Authority(
        target=target,
        owner=owner.strip(),
        allowed_roots=roots,
        allowed_paths=exact,
        allow_leaf_symlink=allow_leaf_symlink,
        expected=expected,
    )


def assert_expected(auth: Authority, expected: PathState | None = None) -> PathState:
    wanted = expected if expected is not None else auth.expected
    if wanted is None:
        raise SafetyError("ownership-unproved", auth.target, "expected state is not sealed")
    current = capture_state(auth.target)
    if current != wanted:
        raise SafetyError(
            "expected-state-mismatch",
            auth.target,
            f"expected={wanted.public()} current={current.public()}",
        )
    return current


def _lock_root() -> Path:
    # The lock namespace must not vary with HOME, CODEX_HOME, ZDOTDIR, or a
    # fixture-specific TMPDIR.  Otherwise two installers can lock different
    # inodes while mutating the same external path.
    uid = os.geteuid() if hasattr(os, "geteuid") else 0
    system_temp = Path("/tmp") if os.name == "posix" else Path(tempfile.gettempdir())
    base = system_temp / f"hearting-path-locks-{uid}"
    parent = base.parent
    parent_info = os.lstat(parent)
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        raise SafetyError("unsafe-ambient-path", parent, "lock parent is unsafe")
    if not base.exists():
        try:
            os.mkdir(base, 0o700)
        except FileExistsError:
            pass
    info = os.lstat(base)
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise SafetyError("ownership-unproved", base, "lock root is not owner-only")
    return base


def lock_path(path: Path | str) -> Path:
    canonical = _canonical_leaf(path, reject_leaf_symlink=False)
    key = hashlib.sha256(os.fsencode(os.fspath(canonical))).hexdigest()
    return _lock_root() / f"{key}.lock"


class TargetLock:
    """Stable advisory lock for one canonical target.

    Lock pathnames are intentionally never unlinked.  Removing them would let a
    waiter retain an old locked inode while another process locks a new inode.
    """

    def __init__(self, target: Path | str):
        self.target = _canonical_leaf(target, reject_leaf_symlink=False)
        _assert_fixture_boundary(self.target)
        self.path: Path | None = None
        self._handle = None

    def __enter__(self) -> "TargetLock":
        self.path = lock_path(self.target)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise SafetyError("ownership-unproved", self.path, "cannot open target lock") from exc
        self._handle = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            if fcntl is not None:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
            opened = os.fstat(self._handle.fileno())
            current = os.lstat(self.path)
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or opened.st_uid != os.geteuid()
                or current.st_uid != os.geteuid()
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            ):
                raise SafetyError("ownership-unproved", self.path, "lock identity is unsafe")
            os.fchmod(self._handle.fileno(), 0o600)
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_args) -> None:
        if self._handle is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


class TargetLocks:
    """Acquire a deterministic, duplicate-free set of target locks."""

    def __init__(self, targets: Iterable[Path | str]):
        self.targets = sorted(
            {_canonical_leaf(item, reject_leaf_symlink=False) for item in targets},
            key=os.fspath,
        )
        for target in self.targets:
            _assert_fixture_boundary(target)
        self._stack: ExitStack | None = None

    def __enter__(self) -> "TargetLocks":
        stack = ExitStack()
        try:
            for target in self.targets:
                stack.enter_context(TargetLock(target))
        except Exception:
            stack.close()
            raise
        self._stack = stack
        return self

    def __exit__(self, *_args) -> None:
        if self._stack is not None:
            self._stack.close()
            self._stack = None


def _fsync_parent(path: Path) -> None:
    try:
        descriptor = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


# destructive-ok: reason=commit a CAS-validated leaf and discard its sibling temp; boundary=authority target plus mkstemp sibling created by this call
def atomic_write_bytes(
    auth: Authority,
    payload: bytes,
    mode: int,
    *,
    expected: PathState | None = None,
    create_parents: bool = False,
) -> PathState:
    """Atomically replace a file/symlink/missing leaf after a second CAS."""

    wanted = expected if expected is not None else auth.expected
    assert_expected(auth, wanted)
    if create_parents:
        auth.target.parent.mkdir(parents=True, exist_ok=True)
        _canonical_leaf(auth.target, reject_leaf_symlink=not auth.allow_leaf_symlink)
    elif not auth.target.parent.is_dir():
        raise SafetyError("unsafe-ambient-path", auth.target.parent, "parent does not exist")
    descriptor, raw_temp = tempfile.mkstemp(
        prefix=f".{auth.target.name}.hearting-", dir=auth.target.parent
    )
    temp = Path(raw_temp)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        assert_expected(auth, wanted)
        os.replace(temp, auth.target)
        _fsync_parent(auth.target)
        return capture_state(auth.target)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        # SAFE_FS_INTERNAL: sibling temp owned by this call; never a caller target.
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


# destructive-ok: reason=commit a CAS-validated symlink and discard its sibling temp; boundary=authority target plus mkstemp sibling created by this call
def atomic_write_symlink(
    auth: Authority,
    target: str,
    *,
    expected: PathState | None = None,
    create_parents: bool = False,
    target_is_directory: bool = False,
) -> PathState:
    wanted = expected if expected is not None else auth.expected
    assert_expected(auth, wanted)
    if not auth.allow_leaf_symlink:
        raise SafetyError("ownership-unproved", auth.target, "leaf symlink policy forbids replacement")
    if create_parents:
        auth.target.parent.mkdir(parents=True, exist_ok=True)
        _canonical_leaf(auth.target, reject_leaf_symlink=False)
    elif not auth.target.parent.is_dir():
        raise SafetyError("unsafe-ambient-path", auth.target.parent, "parent does not exist")
    descriptor, raw_temp = tempfile.mkstemp(
        prefix=f".{auth.target.name}.hearting-", dir=auth.target.parent
    )
    os.close(descriptor)
    temp = Path(raw_temp)
    # SAFE_FS_INTERNAL: convert this call's empty sibling temp to a symlink.
    temp.unlink()
    try:
        temp.symlink_to(target, target_is_directory=target_is_directory)
        assert_expected(auth, wanted)
        os.replace(temp, auth.target)
        _fsync_parent(auth.target)
        return capture_state(auth.target)
    finally:
        # SAFE_FS_INTERNAL: sibling temp owned by this call; never a caller target.
        temp.unlink(missing_ok=True)


# destructive-ok: reason=perform the shared expected-state deletion primitive; boundary=single canonical leaf sealed by operation authority
def remove_exact(
    auth: Authority, *, expected: PathState | None = None, recursive: bool = False
) -> PathState:
    """Remove exactly the sealed leaf; refuse changed state or special files."""

    wanted = expected if expected is not None else auth.expected
    current = assert_expected(auth, wanted)
    if current.kind == "missing":
        return current
    if current.kind in {"file", "symlink"}:
        auth.target.unlink()
    elif current.kind == "directory" and recursive:
        shutil.rmtree(auth.target)
    elif current.kind == "directory":
        auth.target.rmdir()
    else:
        raise SafetyError("ownership-unproved", auth.target, "special leaf cannot be removed")
    _fsync_parent(auth.target)
    return capture_state(auth.target)


def cas_restore(
    auth: Authority, preimage: PathState, postimage: PathState
) -> tuple[str, PathState]:
    """Restore only when current is the sealed postimage.

    A different current object is a concurrent successor even when its bytes
    happen to match.  It is preserved without mutation.
    """

    current = capture_state(auth.target)
    if current == preimage:
        return "already-preimage", current
    if current != postimage:
        raise SafetyError(
            "concurrent-successor",
            auth.target,
            f"postimage={postimage.public()} current={current.public()}",
        )
    current_auth = auth.with_expected(current)
    if preimage.kind == "missing":
        restored = remove_exact(current_auth, recursive=postimage.kind == "directory")
    elif preimage.kind == "file":
        if preimage.payload is None:
            raise SafetyError("ownership-unproved", auth.target, "file preimage payload is absent")
        restored = atomic_write_bytes(
            current_auth, preimage.payload, int(preimage.mode or 0o600)
        )
    elif preimage.kind == "symlink":
        if preimage.link_target is None:
            raise SafetyError("ownership-unproved", auth.target, "symlink preimage is invalid")
        restored = atomic_write_symlink(current_auth, preimage.link_target)
    else:
        raise SafetyError(
            "ownership-unproved", auth.target, f"rollback kind is unsupported: {preimage.kind}"
        )
    return "restored", restored


class Transaction:
    """In-memory pre/postimage transaction holding canonical target locks."""

    def __init__(self, authorities: Mapping[str, Authority]):
        if not authorities:
            raise SafetyError("ownership-unproved", "<transaction>", "target set is empty")
        self.authorities = dict(authorities)
        self.preimages: dict[str, PathState] = {}
        self.postimages: dict[str, PathState] = {}
        self._locks = TargetLocks(auth.target for auth in self.authorities.values())
        self._entered = False
        self._sealed = False

    def __enter__(self) -> "Transaction":
        # Authorities were validated without mutation by their constructors.
        self._locks.__enter__()
        self._entered = True
        try:
            self.preimages = {
                name: capture_state(auth.target, include_payload=True)
                for name, auth in self.authorities.items()
            }
        except Exception:
            self.close()
            raise
        return self

    def seal(self) -> dict[str, PathState]:
        if not self._entered:
            raise SafetyError("ownership-unproved", "<transaction>", "transaction is not open")
        self.postimages = {
            name: capture_state(auth.target) for name, auth in self.authorities.items()
        }
        self._sealed = True
        return dict(self.postimages)

    def restore(self) -> dict[str, str]:
        if not self._sealed:
            raise SafetyError(
                "ownership-unproved", "<transaction>", "transaction postimage is not sealed"
            )
        # Validate the complete rollback set before changing any leaf.  A
        # successor on one path must not let us rewind other paths into an
        # inconsistent partial transaction.
        for name, auth in self.authorities.items():
            current = capture_state(auth.target)
            if current not in {self.preimages[name], self.postimages[name]}:
                raise SafetyError(
                    "concurrent-successor",
                    auth.target,
                    f"postimage={self.postimages[name].public()} "
                    f"current={current.public()}",
                )
        results: dict[str, str] = {}
        for name in reversed(tuple(self.authorities)):
            status_value, restored = cas_restore(
                self.authorities[name], self.preimages[name], self.postimages[name]
            )
            self.preimages[name] = restored
            results[name] = status_value
        return results

    def close(self) -> None:
        if self._entered:
            self._locks.__exit__(None, None, None)
            self._entered = False

    def __exit__(self, *_args) -> None:
        self.close()


def transaction(authorities: Mapping[str, Authority]) -> Transaction:
    return Transaction(authorities)
