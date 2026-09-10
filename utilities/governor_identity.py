#!/usr/bin/env python3
"""Process and file identity primitives for the model-worker governor.

The values in this module are observations, never proofs of another process's
death.  In particular, an unlocked witness is deliberately reported as
unknown rather than dead.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _starttime(pid: int) -> str:
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    tail = raw[raw.rfind(")") + 2 :].split()
    return tail[19]


def _ns_inode(name: str) -> int | None:
    try:
        return os.stat(f"/proc/self/ns/{name}").st_ino
    except OSError:
        return None


def capture_local_identity() -> dict[str, Any]:
    """Capture self twice; incomplete coordinates are never a local boundary."""
    def sample() -> dict[str, Any]:
        pid = os.getpid()
        status = Path("/proc/self/status").read_text(encoding="utf-8")
        fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
        nspid, nspgid = fields["NSpid"].split(), fields["NSpgid"].split()
        if (not nspid or not nspgid or
                not all(x.isdecimal() for x in nspid + nspgid) or
                int(nspid[-1]) != pid or int(nspgid[-1]) != os.getpgrp()):
            raise ValueError("malformed namespace coordinates")
        boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        if not boot:
            raise ValueError("missing boot id")
        return {"pid": pid, "starttime": _starttime(pid), "boot_id": boot,
                "pid_namespace": os.stat("/proc/self/ns/pid").st_ino,
                "NSpid": nspid, "NSpgid": nspgid}
    try:
        before, after = sample(), sample()
        if before != after:
            raise ValueError("identity changed during capture")
        return {**after, "pid1_name": _pid1_name()}
    except (OSError, UnicodeError, ValueError, KeyError, IndexError) as exc:
        raise IdentityCaptureError(f"capture-error:{exc}") from exc


def _pid1_name() -> str | None:
    try:
        raw = Path("/proc/1/stat").read_text(encoding="utf-8")
        return raw[raw.find("(") + 1 : raw.rfind(")")]
    except OSError:
        return None


class IdentityCaptureError(RuntimeError):
    pass


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_all(fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(fd, payload[offset:])
        if written <= 0:
            raise OSError(errno.EIO, "witness payload write made no progress")
        offset += written


@dataclass
class WitnessHandle:
    root: Path
    path: Path
    fd: int
    nonce: str
    phase: str
    identity: dict[str, Any]
    device: int
    inode: int
    payload_sha256: str
    creator_pid: int
    _closed: bool = False

    def binding(self) -> dict[str, Any]:
        return {
            "kind": "governor-flock-v1", "phase": self.phase,
            "relative_path": str(self.path.relative_to(self.root)),
            "nonce": self.nonce, "device": self.device, "inode": self.inode,
            "payload_sha256": self.payload_sha256, "identity": self.identity,
        }


@dataclass(frozen=True)
class IdentityObservation:
    state: str
    reason: str
    binding: dict[str, Any]


def create_witness(root: str | Path, phase: str, identity: dict[str, Any] | None = None) -> WitnessHandle:
    root = Path(root).resolve()
    directory = root / "identity-witnesses"
    directory.mkdir(parents=True, exist_ok=True)
    identity = capture_local_identity() if identity is None else dict(identity)
    for _ in range(20):
        nonce = secrets.token_hex(16)
        path = directory / f"{nonce}.lock"
        payload_obj = {"schema": 1, "nonce": nonce, "phase": phase, "identity": identity}
        payload = (json.dumps(payload_obj, sort_keys=True, separators=(",", ":")) + "\n").encode()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            continue
        try:
            _write_all(fd, payload)
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            st = os.fstat(fd)
            handle = WitnessHandle(root, path, fd, nonce, phase, identity, st.st_dev, st.st_ino, _digest(payload), os.getpid())
            os.register_at_fork(after_in_child=lambda: _child_close(handle))
            return handle
        except BaseException:
            os.close(fd)
            try: path.unlink()
            except OSError: pass
            raise
    raise IdentityCaptureError("witness-create-collision")


def _child_close(handle: WitnessHandle) -> None:
    if not handle._closed:
        try:
            current = os.fstat(handle.fd)
            if (current.st_dev, current.st_ino) == (handle.device, handle.inode):
                os.close(handle.fd)
        except OSError:
            pass
        handle._closed = True


def retained_witness_is_held(handle: WitnessHandle) -> bool:
    """Read kernel evidence for this retained open description, never reacquire.

    PID/ns strings or another process locking the path cannot substitute for
    the original FD. Linux fdinfo reports only locks on this open description.
    Unavailable fdinfo is an unknown result, not a return authorization.
    """
    if handle._closed or handle.creator_pid != os.getpid():
        return False
    try:
        st = os.fstat(handle.fd)
        if (st.st_dev, st.st_ino) != (handle.device, handle.inode):
            return False
        for line in Path(f"/proc/self/fdinfo/{handle.fd}").read_text().splitlines():
            parts = line.split()
            if (len(parts) >= 7 and parts[0] == "lock:" and
                    parts[2:5] == ["FLOCK", "ADVISORY", "WRITE"] and
                    parts[5] == str(handle.creator_pid)):
                return True
    except (OSError, UnicodeError):
        pass
    return False


def close_witness(handle: WitnessHandle) -> None:
    if handle._closed:
        return
    if os.getpid() != handle.creator_pid:
        _child_close(handle)
        return
    if not retained_witness_is_held(handle):
        # The number may have been closed/reused; never close a successor FD.
        handle._closed = True
        return
    try:
        fcntl.flock(handle.fd, fcntl.LOCK_UN)
    finally:
        os.close(handle.fd)
        handle._closed = True
    try:
        st = handle.path.lstat()
        if st.st_dev == handle.device and st.st_ino == handle.inode and stat.S_ISREG(st.st_mode):
            fd = os.open(handle.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                actual = os.fstat(fd)
                payload = os.read(fd, 1 << 20)
                latest = handle.path.lstat()
                if ((actual.st_dev, actual.st_ino) == (handle.device, handle.inode)
                        == (latest.st_dev, latest.st_ino)
                        and _digest(payload) == handle.payload_sha256):
                    handle.path.unlink()
            finally:
                os.close(fd)
    except OSError:
        pass


def prove_witness_unheld(root: str | Path, binding: dict[str, Any]) -> IdentityObservation:
    """Kernel proof that *no* process holds this lease's claimant description.

    `observe_witness` answers "is it held?" and reports an unlocked witness as
    ``unknown``, because an unlocked file says nothing about *why* the holder
    is gone. For returning capacity, why does not matter -- only whether
    anybody still holds it. A witness is created with ``LOCK_EX`` and the lock
    lives on the open file description, so it survives ``fork`` and is released
    by the kernel only when the last descriptor referring to it is closed.
    Taking ``LOCK_EX`` on the exact recorded file therefore proves that the
    claimant and every descendant that inherited its descriptor are gone --
    in any PID namespace, immune to PID reuse, without reading /proc.

    Returns ``unheld``/``exclusive-lock-acquired`` on proof. Every other
    outcome keeps the lease occupied and names why: a replaced, mismatched,
    non-regular or unreadable witness is not evidence of anything, and a
    still-held lock means a live claimant or a live descendant.
    """

    root = Path(root).resolve()
    observed = observe_witness(root, binding)
    if observed.state == "live":
        return observed
    if observed.reason != "witness-unlocked":
        return observed
    try:
        relative = Path(str(binding["relative_path"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("path-outside-root")
        path = root / relative
        path.relative_to(root)
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            return IdentityObservation("unknown", "witness-not-regular", binding)
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            after = os.fstat(fd)
            if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
                return IdentityObservation("unknown", "witness-replaced", binding)
            if after.st_dev != binding.get("device") or after.st_ino != binding.get("inode"):
                return IdentityObservation("unknown", "witness-binding-mismatch", binding)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return IdentityObservation("live", "exact-witness-lock-held", binding)
            # Re-check identity while the exclusive lock is ours: a file
            # swapped between the open and the lock would make the proof
            # describe a different file.
            latest = path.lstat()
            if (latest.st_dev, latest.st_ino) != (after.st_dev, after.st_ino):
                return IdentityObservation("unknown", "witness-replaced", binding)
            return IdentityObservation("unheld", "exclusive-lock-acquired", binding)
        finally:
            os.close(fd)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        return IdentityObservation(
            "unknown", f"witness-unverifiable:{getattr(exc, 'errno', None) or type(exc).__name__}", binding
        )


def observe_witness(root: str | Path, binding: dict[str, Any]) -> IdentityObservation:
    root = Path(root).resolve()
    try:
        relative = Path(str(binding["relative_path"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("path-outside-root")
        path = root / relative
        path.relative_to(root)
        current = root
        for component in relative.parts[:-1]:
            current /= component
            if not stat.S_ISDIR(current.lstat().st_mode):
                raise ValueError("path-symlink-component")
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            return IdentityObservation("unknown", "witness-not-regular", binding)
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            after = os.fstat(fd)
            payload = os.read(fd, 1 << 20)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                return IdentityObservation("unknown", "witness-replaced", binding)
            if after.st_dev != binding.get("device") or after.st_ino != binding.get("inode") or _digest(payload) != binding.get("payload_sha256"):
                return IdentityObservation("unknown", "witness-binding-mismatch", binding)
            decoded = json.loads(payload.decode("utf-8"))
            expected = {
                "schema": 1,
                "nonce": binding.get("nonce"),
                "phase": binding.get("phase"),
                "identity": binding.get("identity"),
            }
            if decoded != expected:
                return IdentityObservation("unknown", "witness-payload-mismatch", binding)
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                latest = path.lstat()
                if (latest.st_dev, latest.st_ino) != (after.st_dev, after.st_ino):
                    return IdentityObservation("unknown", "witness-replaced", binding)
                return IdentityObservation("live", "exact-witness-lock-held", binding)
            return IdentityObservation("unknown", "witness-unlocked", binding)
        finally:
            os.close(fd)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        return IdentityObservation("unknown", f"witness-unverifiable:{getattr(exc, 'errno', None) or type(exc).__name__}", binding)
