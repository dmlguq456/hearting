#!/usr/bin/env python3
"""Hardened Git transport for Hearting memory protocol v2.

The exchange repository is private transport state. Protocol objects are read
and written with Git plumbing; fetched trees are never checked out and
repository-provided hooks, attributes, filters, and merge drivers are never
executed. Remote history is transport evidence only.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
from typing import Callable, Iterable, Mapping
from urllib.parse import unquote, urlparse

from protocol_v2 import (
    MAX_ENVELOPE_BYTES,
    ProtocolError,
    canonical_bytes,
    classify_operations,
    operation_path,
    validate_operation,
)


DEFAULT_REF = "refs/heads/hearting-memory-v2"
FETCH_REF = "refs/hearting/memory/v2/fetched"
INTEGRATION_REF = "refs/hearting/memory/v2/integration"
RENDERED_REF = "refs/hearting/memory/v2/rendered"
GUARD_REF = "refs/hearting/memory/v2/remote-guard"
MAX_TREE_OBJECTS = 1_000_000
MAX_HISTORY_COMMITS = 100_000
MAX_TREE_BYTES = 256 * 1024 * 1024
_OP_PATH = re.compile(
    r"^protocol/v2/ops/([0-9a-f]{2})/([0-9a-f]{64})\.json$"
)


class ExchangeError(RuntimeError):
    status = "hard-failure"
    exit_code = 2


class ExchangeUnavailable(ExchangeError):
    status = "offline"
    exit_code = 1


class RemoteRewind(ExchangeError):
    status = "remote-rewind"


class PushRetryExhausted(ExchangeError):
    status = "retry-exhausted"
    exit_code = 1


class ExchangeBlocked(ExchangeError):
    status = "dependency-blocked"
    exit_code = 1


@dataclass(frozen=True)
class ExchangeSnapshot:
    tip: str | None
    operations: dict[str, object]
    raw_objects: dict[str, bytes]
    classification: object | None = None


@dataclass(frozen=True)
class PublishResult:
    tip: str
    confirmed: tuple[str, ...]
    attempts: int
    snapshot: ExchangeSnapshot | None = None


@dataclass(frozen=True)
class RenderResult:
    commit: str
    op_ids: tuple[str, ...]


class GitExchange:
    """One contained, plumbing-only exchange repository."""

    def __init__(
        self,
        root: Path | str,
        remote: Path | str,
        ref: str = DEFAULT_REF,
        *,
        forbidden_roots: Iterable[Path | str] = (),
        guard_tip: str | None = None,
        max_objects: int = MAX_TREE_OBJECTS,
    ):
        self.root = Path(root).expanduser()
        self.remote = str(remote)
        self.ref = str(ref)
        self.max_objects = int(max_objects)
        if guard_tip is not None and not re.fullmatch(r"[0-9a-f]{40,64}", guard_tip):
            raise ExchangeError("persistent remote guard tip is invalid")
        self.guard_tip = guard_tip
        self._forbidden_roots = [Path(item).expanduser() for item in forbidden_roots]
        self._prepared = False
        self._hooks_dir = self.root / "disabled-hooks"
        if (
            not self.ref.startswith("refs/heads/")
            or ".." in self.ref
            or "@{" in self.ref
            or "//" in self.ref
            or self.ref.endswith(("/", ".", ".lock"))
        ):
            raise ExchangeError("MEM_SYNC_REF must be a full refs/heads/* ref")
        if (
            not self.remote
            or self.remote.startswith("-")
            or any(char in self.remote for char in ("\x00", "\n", "\r"))
        ):
            raise ExchangeError("remote URL is missing or invalid")
        self._validate_location()

    def set_forbidden_roots(self, roots: Iterable[Path | str]) -> None:
        self._forbidden_roots = [Path(item).expanduser() for item in roots]

    @staticmethod
    def _within(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    @staticmethod
    def _git_worktree_ancestor(path: Path) -> Path | None:
        """Return a containing Git worktree root without invoking Git."""
        for candidate in (path, *path.parents):
            marker = candidate / ".git"
            try:
                marker.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ExchangeError(
                    f"cannot inspect synchronized project marker: {marker}"
                ) from exc
            return candidate
        return None

    def _validate_location(self) -> None:
        if not self.root.is_absolute():
            raise ExchangeError("MEM_SYNC_DIR must be absolute")
        probe = Path(self.root.anchor)
        for part in self.root.parts[1:]:
            probe = probe / part
            if probe.exists() and probe.is_symlink():
                raise ExchangeError(f"exchange path contains symlink: {probe}")
        resolved = self.root.resolve(strict=False)
        containing_worktree = self._git_worktree_ancestor(resolved)
        if containing_worktree is not None:
            raise ExchangeError(
                "exchange path is inside a Git project tree: "
                f"{containing_worktree}"
            )
        config_home = Path(
            os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
        ).expanduser().resolve(strict=False)
        forbidden = [config_home]
        forbidden.extend(item.resolve(strict=False) for item in self._forbidden_roots)
        for item in forbidden:
            if resolved == item or self._within(resolved, item):
                raise ExchangeError(f"exchange path is inside a forbidden root: {item}")
        remote_path = self._local_remote_path()
        if remote_path is not None:
            if not remote_path.is_absolute():
                raise ExchangeError("local remote path must be absolute")
            remote_resolved = remote_path.resolve(strict=False)
            remote_worktree = self._git_worktree_ancestor(remote_resolved)
            if remote_worktree is not None:
                raise ExchangeError(
                    "local remote is inside a Git project tree: "
                    f"{remote_worktree}"
                )
            for item in forbidden:
                if remote_resolved == item or self._within(remote_resolved, item):
                    raise ExchangeError(
                        f"local remote is inside a forbidden root: {item}"
                    )

    def _local_remote_path(self) -> Path | None:
        """Resolve a local transport target without treating scp syntax as a path."""
        parsed = urlparse(self.remote)
        if parsed.scheme == "file":
            if parsed.netloc not in ("", "localhost"):
                raise ExchangeError("file remote must not name a remote host")
            return Path(unquote(parsed.path)).expanduser()
        if parsed.scheme:
            if parsed.scheme == "ext":
                raise ExchangeError("ext Git transport is forbidden")
            return None
        if re.match(r"^[^/\\]+@?[^/\\]*:", self.remote):
            return None
        return Path(self.remote).expanduser()

    def _validate_existing_tree(self) -> None:
        """Reject symlinks anywhere below an existing exchange before Git runs."""
        if not self.root.exists():
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)

        def walk(directory_fd: int) -> None:
            for name in os.listdir(directory_fd):
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise ExchangeError(f"exchange repository contains symlink: {name}")
                if stat.S_ISDIR(info.st_mode):
                    child_fd = os.open(name, flags, dir_fd=directory_fd)
                    try:
                        walk(child_fd)
                    finally:
                        os.close(child_fd)
                elif not stat.S_ISREG(info.st_mode):
                    raise ExchangeError(
                        f"exchange repository contains unsafe file type: {name}"
                    )
                elif info.st_nlink != 1:
                    raise ExchangeError(
                        f"exchange repository contains multiply linked file: {name}"
                    )

        try:
            root_fd = os.open(self.root, flags)
            try:
                walk(root_fd)
            finally:
                os.close(root_fd)
        except OSError as exc:
            raise ExchangeError("cannot safely inspect exchange repository") from exc

    def _validate_local_config(self) -> None:
        """Accept only inert bare-repository metadata from local config."""
        path = self.root / "config"
        if not path.exists():
            return
        try:
            info = os.stat(path, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ExchangeError("exchange Git config is not a private regular file")
            parser = configparser.RawConfigParser(
                interpolation=None, strict=True, empty_lines_in_values=False
            )
            with path.open("r", encoding="utf-8", errors="strict") as handle:
                parser.read_file(handle)
        except (OSError, UnicodeError, configparser.Error) as exc:
            raise ExchangeError("exchange Git config is malformed or unreadable") from exc
        allowed = {
            ("core", "repositoryformatversion"): {"0"},
            ("core", "filemode"): {"true", "false"},
            ("core", "bare"): {"true"},
            ("core", "logallrefupdates"): {"true", "false"},
            ("extensions", "objectformat"): {"sha1", "sha256"},
        }
        for section in parser.sections():
            for key, value in parser.items(section, raw=True):
                accepted = allowed.get((section.lower(), key.lower()))
                if accepted is None or value.strip().lower() not in accepted:
                    raise ExchangeError(
                        f"exchange Git config key is not allowlisted: {section}.{key}"
                    )

    def _prepare(self) -> None:
        if self._prepared:
            self._validate_location()
            self._validate_existing_tree()
            self._validate_local_config()
            return
        self._validate_location()
        self._validate_existing_tree()
        self._validate_local_config()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._validate_existing_tree()
        try:
            os.chmod(self.root, stat.S_IRWXU)
        except OSError as exc:
            raise ExchangeError(f"cannot make exchange root private: {exc}") from exc
        if not (self.root / "HEAD").exists():
            self._run("init", "--bare")
            self._run(
                "symbolic-ref", "HEAD", "refs/heads/hearting-memory-v2"
            )
        self._validate_local_config()
        self._hooks_dir.mkdir(mode=0o700, exist_ok=True)
        if any(self._hooks_dir.iterdir()):
            raise ExchangeError("trusted empty hooks directory is not empty")
        for name in ("HEAD", "config", "objects", "refs", "disabled-hooks"):
            if (self.root / name).is_symlink():
                raise ExchangeError(f"exchange repository contains symlink: {name}")
        self._prepared = True

    def _safe_detail(self, result: subprocess.CompletedProcess, fallback: str) -> str:
        lines = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
        detail = lines[-1] if lines else fallback
        detail = detail.replace(self.remote, "<remote>").replace(
            str(self.root), "<exchange>"
        )
        detail = re.sub(r"://[^/@\s]+@", "://***@", detail)
        return detail[-512:]

    def _command_env(self, args: tuple[str, ...]) -> tuple[list[str], dict[str, str]]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(Path.home()),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "GIT_AUTHOR_NAME": "Hearting memory sync",
            "GIT_AUTHOR_EMAIL": "memory-sync@localhost.invalid",
            "GIT_COMMITTER_NAME": "Hearting memory sync",
            "GIT_COMMITTER_EMAIL": "memory-sync@localhost.invalid",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG": "/dev/null",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        askpass = os.environ.get("MEM_SYNC_GIT_ASKPASS")
        if askpass:
            askpass_path = Path(askpass)
            if not askpass_path.is_absolute() or askpass_path.is_symlink():
                raise ExchangeError("MEM_SYNC_GIT_ASKPASS must be an absolute regular path")
            env["GIT_ASKPASS"] = str(askpass_path)
        ssh_command = os.environ.get("MEM_SYNC_GIT_SSH_COMMAND")
        if ssh_command:
            env["GIT_SSH_COMMAND"] = ssh_command
        if os.environ.get("MEM_SYNC_ALLOW_SSH_AGENT") == "1":
            agent_socket = os.environ.get("SSH_AUTH_SOCK")
            if agent_socket:
                env["SSH_AUTH_SOCK"] = agent_socket
        command = [
            "git", "-c", f"core.hooksPath={self._hooks_dir}",
            "-c", "protocol.ext.allow=never",
            "-c", "core.symlinks=false",
            "-c", "core.fsyncObjectFiles=true",
            "-c", "filter.lfs.required=false",
            "--git-dir", str(self.root), *args,
        ]
        return command, env

    def _run(
        self,
        *args: str,
        input_bytes: bytes | None = None,
        check: bool = True,
        timeout: int = 30,
    ) -> subprocess.CompletedProcess:
        command, env = self._command_env(args)
        try:
            result = subprocess.run(
                command, input=input_bytes, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=timeout, check=False, env=env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExchangeUnavailable(f"git transport unavailable: {exc}") from exc
        if check and result.returncode != 0:
            raise ExchangeError(self._safe_detail(result, f"git exited {result.returncode}"))
        return result

    def _rev(self, ref: str) -> str | None:
        result = self._run("rev-parse", "--verify", ref, check=False)
        if result.returncode != 0:
            return None
        value = result.stdout.decode("ascii", errors="strict").strip()
        return value if re.fullmatch(r"[0-9a-f]{40,64}", value) else None

    def _is_ancestor(self, older: str, newer: str) -> bool:
        return self._run(
            "merge-base", "--is-ancestor", older, newer, check=False
        ).returncode == 0

    def _remote_tip(self) -> str | None:
        self._prepare()
        result = self._run(
            "ls-remote", "--refs", self.remote, self.ref, check=False, timeout=30
        )
        if result.returncode != 0:
            raise ExchangeUnavailable(self._safe_detail(result, "remote lookup failed"))
        output = result.stdout.decode("ascii", errors="strict").strip()
        if not output:
            return None
        first = output.splitlines()[0].split()[0]
        if not re.fullmatch(r"[0-9a-f]{40,64}", first):
            raise ExchangeError("remote returned an invalid object id")
        return first

    def _fresh_fetch_tip(self) -> str | None:
        guarded = self._rev(GUARD_REF)
        guards = tuple(
            dict.fromkeys(value for value in (guarded, self.guard_tip) if value)
        )
        for _attempt in range(3):
            advertised = self._remote_tip()
            self._run("update-ref", "-d", FETCH_REF, check=False)
            if advertised is None:
                if guards:
                    raise RemoteRewind("authoritative protected ref disappeared")
                return None
            result = self._run(
                "fetch", "--no-tags", self.remote, f"+{self.ref}:{FETCH_REF}",
                check=False, timeout=60,
            )
            if result.returncode != 0:
                raise ExchangeUnavailable(self._safe_detail(result, "fresh fetch failed"))
            fetched = self._rev(FETCH_REF)
            confirmed_advertisement = self._remote_tip()
            if fetched != confirmed_advertisement:
                continue
            for guard in guards:
                if not self._is_ancestor(guard, fetched):
                    raise RemoteRewind(
                        "authoritative protected ref is not a descendant of guard"
                    )
            return fetched
        raise ExchangeUnavailable("authoritative ref changed during three fresh fetches")

    def _batch_blobs(self, oids: Iterable[str]) -> dict[str, bytes]:
        """Read bounded blobs through one long-lived cat-file process."""

        ordered = tuple(oids)
        if not ordered:
            return {}
        command, env = self._command_env(("cat-file", "--batch"))
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
        except OSError as exc:
            raise ExchangeUnavailable(f"git transport unavailable: {exc}") from exc
        assert process.stdin is not None and process.stdout is not None
        blobs: dict[str, bytes] = {}
        try:
            for expected in ordered:
                process.stdin.write(expected.encode("ascii") + b"\n")
                process.stdin.flush()
                header = process.stdout.readline(256)
                if not header.endswith(b"\n") or len(header) >= 256:
                    raise ExchangeError("invalid Git batch blob header")
                try:
                    actual, kind, raw_size = header[:-1].decode("ascii").split()
                    size = int(raw_size)
                except (UnicodeError, ValueError) as exc:
                    raise ExchangeError("invalid Git batch blob header") from exc
                if actual != expected or kind != "blob" or size < 0:
                    raise ExchangeError("Git batch returned an unexpected object")
                if size > MAX_ENVELOPE_BYTES:
                    raise ExchangeError(
                        f"operation envelope exceeds {MAX_ENVELOPE_BYTES} bytes"
                    )
                chunks = bytearray()
                while len(chunks) < size:
                    chunk = process.stdout.read(size - len(chunks))
                    if not chunk:
                        raise ExchangeError("Git blob ended before its declared size")
                    chunks.extend(chunk)
                if process.stdout.read(1) != b"\n":
                    raise ExchangeError("Git blob framing is invalid")
                blobs[expected] = bytes(chunks)
            process.stdin.close()
            returncode = process.wait(timeout=30)
            if returncode != 0:
                raise ExchangeError("Git batch blob inspection failed")
            return blobs
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.wait()
            raise
        finally:
            if not process.stdin.closed:
                process.stdin.close()
            process.stdout.close()
            assert process.stderr is not None
            process.stderr.close()

    def _z_entries(
        self, *args: str, max_items: int, max_bytes: int
    ) -> list[bytes]:
        """Read a NUL-delimited Git stream with caps enforced while reading."""
        command, env = self._command_env(args)
        try:
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env
            )
        except OSError as exc:
            raise ExchangeUnavailable(f"git transport unavailable: {exc}") from exc
        assert process.stdout is not None
        entries: list[bytes] = []
        pending = b""
        total = 0
        try:
            while True:
                chunk = process.stdout.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    process.kill()
                    raise ExchangeError("Git inspection output exceeds byte limit")
                pending += chunk
                pieces = pending.split(b"\0")
                pending = pieces.pop()
                for item in pieces:
                    if not item:
                        continue
                    entries.append(item)
                    if len(entries) > max_items:
                        process.kill()
                        raise ExchangeError("Git inspection output exceeds item limit")
            returncode = process.wait(timeout=30)
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.wait()
            raise
        finally:
            process.stdout.close()
        if returncode != 0:
            raise ExchangeError("Git streaming inspection failed")
        if pending:
            raise ExchangeError("Git returned an unterminated tree entry")
        return entries

    def _validate_add_only_history(self, tip: str, guard: str | None) -> None:
        revision = f"{guard}..{tip}" if guard else tip
        result = self._run(
            "rev-list", "--reverse", "--topo-order", "--parents",
            f"--max-count={MAX_HISTORY_COMMITS + 1}", revision,
        )
        lines = result.stdout.decode("ascii", errors="strict").splitlines()
        if len(lines) > MAX_HISTORY_COMMITS:
            raise ExchangeError("exchange history exceeds traversal limit")
        commits = []
        for line in lines:
            fields = line.split()
            if not fields or not all(re.fullmatch(r"[0-9a-f]{40,64}", item)
                                     for item in fields):
                raise ExchangeError("invalid exchange commit history")
            commits.append(fields[0])
        if commits:
            changed = self._run(
                "diff-tree", "--stdin", "--root", "-r", "-m",
                "--no-commit-id", "--name-only", "--diff-filter=CDMRTUXB", "-z",
                input_bytes=("\n".join(commits) + "\n").encode("ascii"),
                check=False,
            )
            if changed.returncode != 0:
                raise ExchangeError("cannot validate immutable operation history")
            if changed.stdout:
                raise ExchangeError(
                    "immutable operation history changed or deleted an existing path"
                )

    def _tree(self, tip: str | None) -> ExchangeSnapshot:
        if tip is None:
            return ExchangeSnapshot(None, {}, {})
        chunks = self._z_entries(
            "ls-tree", "-r", "-z", "--full-tree", tip,
            max_items=self.max_objects,
            max_bytes=min(MAX_TREE_BYTES, max(4096, self.max_objects * 192)),
        )
        entries: list[tuple[str, str, str]] = []
        seen_paths: set[str] = set()
        for chunk in chunks:
            try:
                left, path_bytes = chunk.split(b"\t", 1)
                mode, obj_type, oid = left.decode("ascii").split(" ", 2)
                path = path_bytes.decode("ascii")
            except (ValueError, UnicodeDecodeError) as exc:
                raise ExchangeError("invalid exchange tree entry") from exc
            match = _OP_PATH.fullmatch(path)
            if mode != "100644" or obj_type != "blob" or match is None:
                raise ExchangeError(f"unexpected or unsafe tracked path: {path}")
            op_id = match.group(2)
            if match.group(1) != op_id[:2]:
                raise ExchangeError(f"operation shard mismatch: {path}")
            if path in seen_paths:
                raise ExchangeError(f"duplicate operation path: {path}")
            seen_paths.add(path)
            entries.append((path, op_id, oid))
        blobs = self._batch_blobs(oid for _path, _op_id, oid in entries)
        operations: dict[str, object] = {}
        raw_objects: dict[str, bytes] = {}
        raw_total = 0
        for path, op_id, oid in entries:
            raw = blobs[oid]
            raw_total += len(raw)
            if raw_total > MAX_TREE_BYTES:
                raise ExchangeError("exchange operation bytes exceed configured limit")
            try:
                operation = validate_operation(raw, path=path)
            except ProtocolError as exc:
                raise ExchangeError(
                    f"invalid protocol operation: {exc.code}"
                ) from exc
            operations[op_id] = operation
            raw_objects[path] = raw
        classification = classify_operations(operations.values())
        if classification.hard_failures:
            codes = ",".join(item.code for item in classification.hard_failures[:8])
            raise ExchangeError(f"whole-set protocol validation failed: {codes}")
        return ExchangeSnapshot(tip, operations, raw_objects, classification)

    def fetch_validate(self) -> ExchangeSnapshot:
        self._prepare()
        self._validate_location()
        guard = self._rev(GUARD_REF)
        tip = self._fresh_fetch_tip()
        if tip is not None:
            self._validate_add_only_history(tip, guard)
        snapshot = self._tree(tip)
        return snapshot

    fetch_and_validate = fetch_validate
    fetch = fetch_validate

    def _write_tree(self, raw_objects: Mapping[str, bytes]) -> str:
        nested: dict[str, object] = {}
        for path, raw in raw_objects.items():
            node = nested
            parts = path.split("/")
            for part in parts[:-1]:
                child = node.setdefault(part, {})
                if not isinstance(child, dict):
                    raise ExchangeError("Git tree prefix collision")
                node = child
            if parts[-1] in node:
                raise ExchangeError("duplicate Git tree leaf")
            node[parts[-1]] = raw

        def write(node: Mapping[str, object]) -> str:
            entries: list[bytes] = []
            for name in sorted(node, key=lambda item: item.encode("utf-8")):
                value = node[name]
                if isinstance(value, dict):
                    oid = write(value)
                    entries.append(f"040000 tree {oid}\t{name}".encode() + b"\0")
                else:
                    assert isinstance(value, bytes)
                    oid = self._run(
                        "hash-object", "-w", "--stdin", input_bytes=value
                    ).stdout.decode("ascii").strip()
                    entries.append(f"100644 blob {oid}\t{name}".encode() + b"\0")
            return self._run(
                "mktree", "-z", input_bytes=b"".join(entries)
            ).stdout.decode("ascii").strip()

        return write(nested)

    def _commit_union(self, parent: str | None, objects: Mapping[str, bytes]) -> str:
        tree = self._write_tree(objects)
        previous = self._rev(INTEGRATION_REF)
        parents = [parent] if parent else []
        if previous and previous != parent:
            if parent is None or not self._is_ancestor(previous, parent):
                parents.append(previous)
        if parent:
            current_tree = self._run(
                "rev-parse", f"{parent}^{{tree}}"
            ).stdout.decode("ascii").strip()
            if current_tree == tree and parents == [parent]:
                self._run("update-ref", INTEGRATION_REF, parent)
                return parent
        elif previous:
            previous_tree = self._run(
                "rev-parse", f"{previous}^{{tree}}"
            ).stdout.decode("ascii").strip()
            if previous_tree == tree:
                return previous
        args = ["commit-tree", tree]
        for commit_parent in parents:
            args.extend(["-p", commit_parent])
        commit = self._run(
            *args, input_bytes=b"hearting memory v2 immutable operation union\n"
        ).stdout.decode("ascii").strip()
        self._run("update-ref", INTEGRATION_REF, commit)
        return commit

    def _render_local(self, objects: Mapping[str, bytes]) -> str:
        """Make local operation bytes durable and reachable before remote I/O."""
        self._write_render_evidence(objects)
        tree = self._write_tree(objects)
        commit = self._run(
            "commit-tree", tree,
            input_bytes=b"hearting memory v2 durable rendered operations\n",
        ).stdout.decode("ascii").strip()
        self._run("update-ref", RENDERED_REF, commit)
        rendered = self._tree(commit)
        if rendered.raw_objects != dict(objects):
            raise ExchangeError("rendered operation evidence differs from local bytes")
        self._fsync_render_evidence(commit)
        return commit

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _fsync_render_evidence(self, commit: str) -> None:
        """Reopen and fsync the rendered ref and all containing directories."""
        ref_path = self.root / RENDERED_REF
        try:
            info = os.stat(ref_path, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ExchangeError("rendered Git ref is not a private regular file")
            fd = os.open(ref_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            directories = {self.root / "objects"}
            cursor = ref_path.parent
            while True:
                directories.add(cursor)
                if cursor == self.root:
                    break
                cursor = cursor.parent
            for directory in sorted(
                directories,
                key=lambda item: len(item.parts), reverse=True,
            ):
                self._fsync_directory(directory)
        except OSError as exc:
            raise ExchangeError("cannot fsync rendered Git ref evidence") from exc

    def _write_render_evidence(self, objects: Mapping[str, bytes]) -> None:
        """Atomically persist canonical op bytes independently of Git packing."""
        evidence_dir = self.root / "rendered-evidence"
        evidence_dir.mkdir(mode=0o700, exist_ok=True)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            directory_fd = os.open(evidence_dir, flags)
        except OSError as exc:
            raise ExchangeError("cannot open rendered evidence directory safely") from exc
        try:
            for path, raw in sorted(objects.items()):
                match = _OP_PATH.fullmatch(path)
                if match is None:
                    raise ExchangeError("rendered evidence path is invalid")
                name = f"{match.group(2)}.json"
                temporary = f".{name}.{secrets.token_hex(8)}.tmp"
                create_flags = (
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                fd = os.open(temporary, create_flags, 0o600, dir_fd=directory_fd)
                try:
                    view = memoryview(raw)
                    while view:
                        written = os.write(fd, view)
                        if written <= 0:
                            raise OSError("short rendered evidence write")
                        view = view[written:]
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(
                    temporary, name,
                    src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                )
                os.fsync(directory_fd)
                verify_fd = os.open(
                    name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
                try:
                    info = os.fstat(verify_fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise ExchangeError("rendered evidence is not a private regular file")
                    chunks = []
                    while True:
                        chunk = os.read(verify_fd, 65536)
                        if not chunk:
                            break
                        chunks.append(chunk)
                    if b"".join(chunks) != raw:
                        raise ExchangeError("rendered evidence bytes changed after rename")
                finally:
                    os.close(verify_fd)
        except OSError as exc:
            raise ExchangeError("cannot persist rendered operation evidence") from exc
        finally:
            os.close(directory_fd)

    def _validated_local_objects(
        self, operations: Iterable[Mapping]
    ) -> tuple[dict[str, bytes], set[str]]:
        local: dict[str, bytes] = {}
        op_ids: set[str] = set()
        for operation in operations:
            validated = validate_operation(dict(operation))
            op_id = validated.op_id
            path = operation_path(op_id)
            raw = validated.raw
            previous = local.get(path)
            if previous is not None and previous != raw:
                raise ExchangeError(f"local immutable path collision: {path}")
            local[path] = raw
            op_ids.add(op_id)
        return local, op_ids

    def render_operations(
        self,
        operations: Iterable[Mapping],
        *,
        phase_callback: Callable[[str, str, tuple[str, ...]], None] | None = None,
    ) -> RenderResult:
        local, op_ids = self._validated_local_objects(operations)
        if not local:
            return RenderResult("", ())
        self._prepare()
        commit = self._render_local(local)
        ordered_ids = tuple(sorted(op_ids))
        if phase_callback is not None:
            phase_callback("rendered", commit, ordered_ids)
        return RenderResult(commit, ordered_ids)

    def publish_operations(
        self,
        operations: Iterable[Mapping],
        *,
        max_attempts: int = 3,
        phase_callback: Callable[[str, str, tuple[str, ...]], None] | None = None,
        fold_callback: Callable[[ExchangeSnapshot], None] | None = None,
        initial_snapshot: ExchangeSnapshot | None = None,
    ) -> PublishResult:
        if not 1 <= int(max_attempts) <= 3:
            raise ExchangeError("push retry count must be between 1 and 3")
        local, op_ids = self._validated_local_objects(operations)
        if not local:
            snapshot = initial_snapshot or self.fetch_validate()
            classification = snapshot.classification
            if classification is not None and (
                classification.deferred or classification.quarantined
            ):
                raise ExchangeBlocked(
                    "deferred or quarantined operation prevents publish completion"
                )
            return PublishResult(snapshot.tip or "", (), 0, snapshot)
        self._prepare()
        rendered_commit = self._render_local(local)
        if phase_callback is not None:
            phase_callback("rendered", rendered_commit, tuple(sorted(op_ids)))
        last_detail = "push rejected"
        for attempt in range(1, max_attempts + 1):
            snapshot = (
                initial_snapshot if attempt == 1 and initial_snapshot is not None
                else self.fetch_validate()
            )
            union = dict(snapshot.raw_objects)
            for path, raw in local.items():
                previous = union.get(path)
                if previous is not None and previous != raw:
                    raise ExchangeError(f"remote immutable path collision: {path}")
                union[path] = raw
            union_ops = [
                validate_operation(raw, path=path)
                for path, raw in sorted(union.items())
            ]
            classification = classify_operations(union_ops)
            if classification.hard_failures:
                codes = ",".join(item.code for item in classification.hard_failures[:8])
                raise ExchangeError(f"whole-set protocol validation failed: {codes}")
            if classification.deferred or classification.quarantined:
                raise ExchangeBlocked(
                    "deferred or quarantined operation prevents push and confirmation"
                )
            commit = self._commit_union(snapshot.tip, union)
            if phase_callback is not None:
                phase_callback("committed", commit, tuple(sorted(op_ids)))
            if fold_callback is not None:
                fold_callback(ExchangeSnapshot(
                    commit,
                    {operation.op_id: operation for operation in union_ops},
                    union,
                    classification,
                ))
            if snapshot.tip == commit:
                confirmed, authoritative = self._fresh_confirm_many(op_ids)
                return PublishResult(commit, confirmed, attempt, authoritative)
            result = self._run(
                "push", "--porcelain", self.remote, f"{commit}:{self.ref}",
                check=False, timeout=60,
            )
            if result.returncode == 0:
                confirmed, authoritative = self._fresh_confirm_many(op_ids)
                return PublishResult(commit, confirmed, attempt, authoritative)
            last_detail = self._safe_detail(result, "fast-forward push rejected")
        raise PushRetryExhausted(last_detail or "fast-forward push retry exhausted")

    publish = publish_operations
    sync = publish_operations

    def _fresh_confirm_many(
        self, op_ids: Iterable[str]
    ) -> tuple[tuple[str, ...], ExchangeSnapshot]:
        expected = tuple(sorted(set(op_ids)))
        snapshot = self.fetch_validate()
        classification = snapshot.classification
        if classification is not None and (
            classification.deferred or classification.quarantined
        ):
            raise ExchangeBlocked(
                "deferred or quarantined operation prevents fresh confirmation"
            )
        confirmed = tuple(
            op_id for op_id in expected if operation_path(op_id) in snapshot.raw_objects
        )
        if confirmed == expected and snapshot.tip is not None:
            old = self._rev(GUARD_REF)
            args = ["update-ref", GUARD_REF, snapshot.tip]
            if old:
                args.append(old)
            self._run(*args)
        return confirmed, snapshot

    def fresh_confirm(self, op_id: str) -> bool:
        confirmed, _snapshot = self._fresh_confirm_many((op_id,))
        return op_id in confirmed

    def confirm_validated_snapshot(
        self, snapshot: ExchangeSnapshot
    ) -> ExchangeSnapshot:
        """Advance rewind evidence from an already fresh validated snapshot."""

        classification = snapshot.classification
        if classification is not None and (
            classification.deferred or classification.quarantined
        ):
            raise ExchangeBlocked(
                "deferred or quarantined operation prevents snapshot confirmation"
            )
        if snapshot.tip is not None:
            old = self._rev(GUARD_REF)
            args = ["update-ref", GUARD_REF, snapshot.tip]
            if old:
                args.append(old)
            self._run(*args)
        return snapshot

    def confirm_snapshot(self, expected_tip: str | None) -> ExchangeSnapshot:
        """Freshly confirm the exact folded snapshot and advance rewind evidence."""
        snapshot = self.fetch_validate()
        if snapshot.tip != expected_tip:
            raise ExchangeUnavailable(
                "authoritative ref changed after fold; retry before confirmation"
            )
        return self.confirm_validated_snapshot(snapshot)

    confirm_operation = fresh_confirm
    confirm = fresh_confirm


__all__ = [
    "DEFAULT_REF",
    "ExchangeError",
    "ExchangeBlocked",
    "ExchangeSnapshot",
    "ExchangeUnavailable",
    "GitExchange",
    "PublishResult",
    "PushRetryExhausted",
    "RemoteRewind",
]
