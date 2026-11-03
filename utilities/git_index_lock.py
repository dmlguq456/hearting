#!/usr/bin/env python3
"""Bounded, read-only diagnosis of Git index-lock failures."""
from __future__ import annotations

import argparse
import base64
import io
import json
import locale
import os
from pathlib import Path
import re
import shlex
import select
import signal
import stat
import subprocess
import sys
import time
try:  # ends a detached child with the test that started it; no-op outside tests
    from detached_lifetime import bind as end_with_test
except ImportError:  # a partial tree without it (e.g. a fixture release): no record
    def end_with_test(child):
        return child

DIAGNOSIS_BUDGET_SECONDS = 2.0
_LOCK_ERROR = re.compile(
    rb"unable to create ['\"](?P<path>[^\r\n]*?index\.lock)['\"]:\s*file exists\b",
    re.IGNORECASE,
)
_LOCK_ERROR_TEXT = re.compile(
    r"unable to create ['\"](?P<path>[^\r\n]*?index\.lock)['\"]:\s*file exists\b",
    re.IGNORECASE,
)
_SHELL_META = set(";&|<>$()\n\r") | {chr(96)}
_GIT_ENV_PATHS = {
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
    "GIT_CEILING_DIRECTORIES",
}
_GIT_CONFIG_FILE_SELECTORS = {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"}
_RESPONSE_CODES = ("exit_code", "exitCode", "returncode", "code")


def compatibility_text(raw: bytes, encoding: str, *, failed: bool) -> str:
    """Match text-mode newline conversion; preserve undecodable failures visibly."""
    errors = "replace" if failed else "strict"
    with io.TextIOWrapper(io.BytesIO(raw), encoding=encoding, errors=errors, newline=None) as stream:
        return stream.read()


def failure_record(returncode: int, stdout: bytes, stderr: bytes, encoding: str,
                   *, raw_bytes_available: bool = True) -> dict:
    return {
        "returncode": int(returncode),
        "stdout_b64": base64.b64encode(stdout).decode("ascii"),
        "stderr_b64": base64.b64encode(stderr).decode("ascii"),
        "encoding": encoding,
        "raw_bytes_available": bool(raw_bytes_available),
    }


def _shell_words(command: str) -> list[str] | None:
    if not command or any(ord(char) < 32 and char not in "\t" for char in command):
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>$()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        words = list(lexer)
    except ValueError:
        return None
    if any(word in {";", "|", "||", "|&", ">", ">>", "<", "<<", "$", "(", ")"}
           for word in words):
        return None
    return words


def parse_shell_command(command: str, cwd: str) -> tuple[list[str], str, dict[str, str]] | None:
    """Parse one Git command with literal cwd/env prefixes; never execute it."""
    words = _shell_words(command)
    if not words:
        return None
    base = os.path.abspath(cwd or os.getcwd())
    if "&&" in words:
        if words.count("&&") != 1 or len(words) < 4 or words[0] != "cd":
            return None
        split = words.index("&&")
        if split != 2:
            return None
        base = os.path.abspath(os.path.join(base, words[1]))
        words = words[3:]
    elif any(word in {"&&", "&", ";"} for word in words):
        return None

    env: dict[str, str] = {}
    if words and words[0] == "env":
        words = words[1:]
    while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
        key, value = words.pop(0).split("=", 1)
        env[key] = value
    if not words or Path(words[0]).name != "git":
        return None
    argv = [words[0], *words[1:]]
    if any(any(char in _SHELL_META for char in word) for word in argv):
        return None
    return argv, base, env


def _global_args(argv: list[str]) -> list[str] | None:
    if not argv or Path(argv[0]).name != "git":
        return None
    result = [argv[0]]
    index = 1
    takes_value = {"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                   "--super-prefix", "--exec-path"}
    flags = {"--bare", "--no-optional-locks", "--literal-pathspecs",
             "--no-replace-objects", "--paginate", "--no-pager"}
    while index < len(argv):
        token = argv[index]
        if token in takes_value:
            if index + 1 >= len(argv):
                return None
            result.extend((token, argv[index + 1]))
            index += 2
            continue
        if any(token.startswith(option + "=") for option in
               ("--git-dir", "--work-tree", "--namespace", "--super-prefix", "--exec-path")):
            result.append(token)
            index += 1
            continue
        if token in flags:
            result.append(token)
            index += 1
            continue
        if token.startswith("-"):
            return None
        return result
    return None


def _run_git_path(global_args: list[str], cwd: str, env: dict[str, str],
                  path: str, timeout: float) -> str | None:
    if timeout <= 0:
        return None
    try:
        child = subprocess.Popen(
            [*global_args, "rev-parse", "--git-path", path],
            cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        output, _ = child.communicate()
    except OSError:
        return None
    if child.returncode:
        return None
    value = os.fsdecode(output).rstrip("\r\n")
    return value if value and "\n" not in value and "\r" not in value else None


def _effective_git_cwd(argv: list[str], cwd: str) -> str | None:
    """Apply Git's ordered -C changes before resolving relative index paths."""
    global_args = _global_args(argv)
    if global_args is None:
        return None
    current = os.path.realpath(os.path.abspath(cwd or os.getcwd()))
    index = 1
    while index < len(global_args):
        token = global_args[index]
        if token == "-C":
            value = global_args[index + 1]
            current = os.path.realpath(
                value if os.path.isabs(value) else os.path.join(current, value)
            )
            if not os.path.isdir(current):
                return None
            index += 2
        else:
            index += 1
    return current


def _run_git_index(argv: list[str], cwd: str, env: dict[str, str], timeout: float):
    global_args = _global_args(argv)
    if global_args is None or timeout <= 0:
        return None
    effective_cwd = _effective_git_cwd(argv, cwd)
    if effective_cwd is None:
        return None
    deadline = time.monotonic() + timeout
    index_path = _run_git_path(
        global_args, cwd, env, "index", max(0.0, deadline - time.monotonic())
    )
    logical_lock = _run_git_path(
        global_args, cwd, env, "index.lock", max(0.0, deadline - time.monotonic())
    )
    if not index_path or not logical_lock:
        return None
    # git < 2.31 has no --path-format=absolute, so bind relative answers here.
    index_path, logical_lock = (
        os.path.realpath(os.path.join(effective_cwd, value))
        for value in (index_path, logical_lock)
    )
    return {"index": index_path, "logical_lock": logical_lock, "cwd": effective_cwd}


def errno_name(exc: OSError) -> str:
    return {1: "permission-denied", 2: "not-found", 13: "permission-denied"}.get(
        exc.errno, "observation-error")


def _observe(path: str, timeout: float) -> dict:
    """Observe directly; production calls run inside the bounded worker group."""
    if timeout <= 0:
        return {"state": "unavailable", "reason": "deadline-exhausted"}
    try:
        before = os.lstat(path)
        after = os.lstat(path)
    except FileNotFoundError:
        return {"state": "not_found"}
    except OSError as exc:
        return {"state": "unavailable", "reason": errno_name(exc)}
    before_id = (before.st_dev, before.st_ino, stat.S_IFMT(before.st_mode))
    after_id = (after.st_dev, after.st_ino, stat.S_IFMT(after.st_mode))
    if before_id != after_id:
        return {"state": "replaced"}
    mode = after.st_mode
    kind = "symlink" if stat.S_ISLNK(mode) else "regular-file" if stat.S_ISREG(mode) else (
        "directory" if stat.S_ISDIR(mode) else "other")
    return {
        "state": "present", "kind": kind, "mode": oct(stat.S_IMODE(mode)),
        "size": after.st_size, "mtime_ns": after.st_mtime_ns,
    }


def _lock_path_from_error(stderr: bytes) -> str | None:
    match = _LOCK_ERROR.search(stderr)
    return os.fsdecode(match.group("path")) if match else None


def _context_env(extra: dict[str, str] | None) -> dict[str, str]:
    env = os.environ.copy()
    if extra:
        for key, value in extra.items():
            if key in _GIT_ENV_PATHS or key.startswith("GIT_CONFIG_"):
                env[key] = value
    return env


def _read_only_commands(argv: list[str], cwd: str, env: dict[str, str], lock_path: str) -> list[str]:
    global_args = _global_args(argv)
    commands = [f"ls -ld -- {shlex.quote(lock_path)}"]
    if global_args is None or any(token == "-c" or token.startswith("-c=") for token in global_args):
        return commands
    if any(key.startswith("GIT_CONFIG") for key in env):
        return commands
    safe_env = [(key, env[key]) for key in sorted(_GIT_ENV_PATHS) if env.get(key)]
    git_command = ["git", "--no-optional-locks", *global_args[1:], "status", "--short", "--branch"]
    prefix = "env" + "".join(f" {key}={shlex.quote(value)}" for key, value in safe_env)
    status = f"cd {shlex.quote(os.path.abspath(cwd))} && {prefix} " + " ".join(
        shlex.quote(value) for value in git_command)
    commands.insert(0, status)
    return commands


def _diagnose_bound_path(argv: list[str], cwd: str, returncode: int, reported_path: str,
                         *, env: dict[str, str] | None, deadline: float,
                         observer=None, git_runner=None,
                         output_source: str = "Git stderr") -> dict:
    process_env = _context_env(env)
    runner = git_runner or _run_git_index
    resolved = runner(argv, cwd, process_env, max(0.0, deadline - time.monotonic()))
    if not resolved:
        return {"state": "unbound", "message": (
            "Could not bind the reported index.lock failure to this Git context; no path was inspected.")}
    index_path = resolved.get("index") if isinstance(resolved, dict) else str(resolved)
    if not isinstance(index_path, str) or not index_path:
        return {"state": "unbound", "message": (
            "Could not bind the reported index.lock failure to this Git context; no path was inspected.")}
    index_path = os.path.abspath(index_path)
    selected_lock = index_path + ".lock"
    effective_cwd = (resolved.get("cwd") if isinstance(resolved, dict) else None)
    effective_cwd = effective_cwd or _effective_git_cwd(argv, cwd) or os.path.abspath(cwd)
    explicit_index = process_env.get("GIT_INDEX_FILE")
    if explicit_index is not None and not explicit_index:
        return {"state": "unbound", "message": (
            "The selected Git index path is ambiguous; no path was inspected.")}

    allowed_reports = {selected_lock}
    if explicit_index:
        logical_index = (explicit_index if os.path.isabs(explicit_index) else
                         os.path.join(effective_cwd, explicit_index))
        logical_index = os.path.abspath(logical_index)
        try:
            same_index = os.path.realpath(logical_index) == os.path.realpath(index_path)
        except OSError:
            same_index = False
        if not same_index:
            return {"state": "unbound", "message": (
                "The selected Git index path is ambiguous; no path was inspected.")}
        allowed_reports.add(logical_index + ".lock")
    elif isinstance(resolved, dict):
        logical_lock = resolved.get("logical_lock")
        if isinstance(logical_lock, str) and logical_lock.endswith(".lock"):
            logical_index = os.path.abspath(logical_lock[:-5])
            try:
                same_index = os.path.realpath(logical_index) == os.path.realpath(index_path)
            except OSError:
                same_index = False
            if same_index:
                allowed_reports.add(os.path.abspath(logical_lock))

    reported = (reported_path if os.path.isabs(reported_path) else
                os.path.abspath(os.path.join(effective_cwd, reported_path)))
    allowed_normalized = {os.path.normcase(os.path.abspath(path)) for path in allowed_reports}
    if os.path.normcase(os.path.abspath(reported)) not in allowed_normalized:
        return {"state": "unbound", "message": (
            "The reported lock path differs from this Git context; no path was inspected.")}

    # Git may report a logical index.lock label for a symlinked index. Both
    # accepted labels are bound to this one selected index; observe beside the
    # path Git resolved, and never follow a lock symlink.
    expected = selected_lock
    observe = observer or _observe
    observation = observe(expected, max(0.0, deadline - time.monotonic()))
    if not isinstance(observation, dict):
        observation = {"state": "unavailable", "reason": "observer-invalid-result"}
    cleanup = {"outcome": "not-attempted", "reason": "read-only-diagnosis"}
    state = observation.get("state", "unavailable")
    if state == "present" and observation.get("kind") == "symlink":
        observed = "present (symlink; target not followed)"
    elif state == "present":
        observed = (f"present ({observation.get('kind', 'unknown')}, mode {observation.get('mode', 'unknown')}, "
                    f"{observation.get('size', 'unknown')} bytes, mtime_ns {observation.get('mtime_ns', 'unknown')})")
    elif state == "not_found":
        observed = "not found at observation time"
    elif state == "replaced":
        observed = "changed during observation; stable attributes withheld"
    else:
        observed = f"unavailable ({observation.get('reason', 'unknown reason')})"
    checks = _read_only_commands(argv, cwd, process_env, expected)
    check_lines = "\n".join(f"  {command}" for command in checks)
    source_note = (
        f"Output source: {output_source}; matching text is advisory and does not prove Git returned EEXIST. "
        "Stdout/stderr separation or raw bytes may be unavailable.\n"
        if output_source != "Git stderr" else ""
    )
    ownership_note = (
        "Ownership: unknown. Mode, size, timestamps, cache age, and local process visibility "
        "do not exclude an outstanding NFS client operation.\n"
    )
    cleanup_note = (
        "Lock mutation: none. This bounded diagnosis never renames, unlinks, quarantines, "
        "restores, retries, or replays the failed command.\n"
    )
    return {
        "state": state, "lock_path": expected, "observation": observation,
        "cleanup": cleanup,
        "message": (
            "Hearting Git index-lock diagnosis\n"
            f"Index lock: {expected}\n"
            f"{source_note}"
            f"Observation: {observed}\n"
            f"{ownership_note}"
            f"{cleanup_note}"
            "Review active work in this worktree. Optional read-only checks:\n"
            f"{check_lines}"
        ),
    }


def _worker_unavailable(reason: str, description: str) -> dict:
    return {
        "state": "unavailable",
        "observation": {"state": "unavailable", "reason": reason},
        "message": ("Hearting Git index-lock diagnosis\n"
                    f"Observation: unavailable ({description})\n"
                    "Ownership: unknown. The original Git failure is unchanged. "
                    "Diagnosis is read-only; no lock mutation or Git retry was attempted."),
    }


def _bounded_worker(payload: dict, deadline: float) -> dict | None:
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > 256 * 1024:
        return _worker_unavailable("diagnostic-input-too-large", "diagnostic input exceeded its bound")
    try:
        child = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--diagnose-worker"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True, bufsize=0,
        )
    except OSError:
        return _worker_unavailable("diagnostic-start-failed", "diagnostic worker did not start")
    end_with_test(child)

    def stop_group() -> None:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except OSError:
            pass
        # Reap promptly after the signal, with a strict short bound. A worker
        # in an uninterruptible kernel wait never holds the caller indefinitely.
        child.poll()
        if child.returncode is None:
            try:
                child.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                pass

    try:
        if child.stdin is None or child.stdout is None:
            stop_group()
            return _worker_unavailable("diagnostic-pipe-unavailable", "diagnostic worker pipe unavailable")
        input_fd = child.stdin.fileno()
        os.set_blocking(input_fd, False)
        pending_input = memoryview(encoded)
        while pending_input:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stop_group()
                return _worker_unavailable("diagnostic-timeout", "two-second diagnostic budget expired")
            _, writable, _ = select.select([], [child.stdin], [], remaining)
            if not writable:
                stop_group()
                return _worker_unavailable("diagnostic-timeout", "two-second diagnostic budget expired")
            try:
                written = os.write(input_fd, pending_input)
            except BlockingIOError:
                continue
            pending_input = pending_input[written:]
        child.stdin.close()
        output = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stop_group()
                return _worker_unavailable("diagnostic-timeout", "two-second diagnostic budget expired")
            ready, _, _ = select.select([child.stdout], [], [], remaining)
            if not ready:
                stop_group()
                return _worker_unavailable("diagnostic-timeout", "two-second diagnostic budget expired")
            chunk = os.read(child.stdout.fileno(), 65537)
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > 65536:
                stop_group()
                return _worker_unavailable("diagnostic-result-too-large", "worker result exceeded its bound")
        remaining = max(0.0, deadline - time.monotonic())
        try:
            child.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            stop_group()
            return _worker_unavailable("diagnostic-timeout", "two-second diagnostic budget expired")
        if child.returncode != 0:
            return _worker_unavailable("diagnostic-worker-failed", "diagnostic worker exited without a result")
        result = json.loads(output)
        return result if isinstance(result, dict) else _worker_unavailable(
            "diagnostic-worker-invalid-result", "diagnostic worker returned an invalid result")
    except (OSError, ValueError, subprocess.SubprocessError):
        stop_group()
        return _worker_unavailable("diagnostic-worker-failed", "diagnostic worker could not finish")
    finally:
        if child.stdin is not None:
            try:
                child.stdin.close()
            except OSError:
                pass
        if child.stdout is not None:
            try:
                child.stdout.close()
            except OSError:
                pass


def _diagnose_payload(payload: dict) -> dict | None:
    try:
        return _diagnose_bound_path(
            payload["argv"], payload["cwd"], payload["returncode"], payload["reported_path"],
            env=payload.get("env"), deadline=payload["deadline"],
            output_source=payload.get("output_source", "Git stderr"),
        )
    except (KeyError, TypeError, ValueError, OSError):
        return None


def diagnose_failure(argv: list[str], cwd: str, returncode: int, stdout: bytes, stderr: bytes,
                     *, env: dict[str, str] | None = None,
                     budget_seconds: float = DIAGNOSIS_BUDGET_SECONDS,
                     observer=None, git_runner=None,
                     output_source: str = "Git stderr") -> dict | None:
    """Return a bounded, read-only observation for a context-bound lock error."""
    if returncode <= 0 or not isinstance(stderr, bytes):
        return None
    reported_path = _lock_path_from_error(stderr)
    if not reported_path:
        return None
    try:
        budget = max(0.0, min(float(budget_seconds), DIAGNOSIS_BUDGET_SECONDS))
    except (TypeError, ValueError):
        budget = 0.0
    deadline = time.monotonic() + budget
    payload = {"argv": argv, "cwd": cwd, "returncode": int(returncode),
               "reported_path": reported_path, "env": env, "deadline": deadline,
               "output_source": output_source}
    # Injected callbacks are deterministic unit seams, not production timeout evidence.
    if observer is not None or git_runner is not None:
        try:
            return _diagnose_bound_path(
                argv, cwd, int(returncode), reported_path, env=env, deadline=deadline,
                observer=observer, git_runner=git_runner, output_source=output_source,
            )
        except (TypeError, ValueError, OSError):
            return None
    return _bounded_worker(payload, deadline)


def _codex_command_argv(command: str, cwd: str):
    words = _shell_words(command)
    if not words:
        return None
    if Path(words[0]).name in {"zsh", "bash", "sh"}:
        if len(words) != 3 or words[1] != "-lc":
            return None
        command = words[2]
    return parse_shell_command(command, cwd)


def _fallback_worktree_context(cwd: str, env: dict[str, str] | None = None):
    """Select only the dispatch worktree (or exact absolute tool cwd) default index."""
    source = os.environ if env is None else env
    if any(source.get(key) for key in _GIT_ENV_PATHS):
        return None
    # GIT_CONFIG_GLOBAL/SYSTEM/NOSYSTEM only pick config files, like ~/.gitconfig;
    # injected settings (GIT_CONFIG_COUNT/KEY_n/VALUE_n/PARAMETERS) stay unsupported.
    if any(key.startswith("GIT_CONFIG_") and key not in _GIT_CONFIG_FILE_SELECTORS and value
           for key, value in source.items()):
        return None
    dispatched = source.get("AGENT_DISPATCH_WORKTREE")
    if dispatched:
        if not os.path.isabs(dispatched):
            return None
        selected = os.path.realpath(dispatched)
    else:
        if not isinstance(cwd, str) or not os.path.isabs(cwd):
            return None
        selected = os.path.realpath(cwd)
    if not os.path.isdir(selected):
        return None
    return ["git", "-C", selected, "status"], selected, {}


def _shell_failure_candidate(command: str, cwd: str, exit_code: int,
                             output: str, parse_command, env: dict[str, str] | None = None):
    """Cheaply recognize a structured, supported index-lock failure candidate."""
    if (not isinstance(command, str) or len(command) > 64 * 1024
            or not isinstance(cwd, str) or len(cwd) > 16 * 1024 or not os.path.isabs(cwd)
            or not isinstance(exit_code, int) or isinstance(exit_code, bool)
            or exit_code <= 0 or exit_code > 128
            or not isinstance(output, str) or len(output) > 1_000_000):
        return None
    match = _LOCK_ERROR_TEXT.search(output)
    parsed = parse_command(command, cwd)
    if not match:
        return None
    if parsed is None:
        parsed = _fallback_worktree_context(cwd, env)
    if parsed is None:
        return None
    return parsed, match.group("path")


def codex_command_candidate(command: str, cwd: str, exit_code: int,
                            aggregated_output: str, *, env: dict[str, str] | None = None) -> bool:
    """Return whether a native App Server item merits bounded path diagnosis."""
    return _shell_failure_candidate(
        command, cwd, exit_code, aggregated_output, _codex_command_argv, env,
    ) is not None


def _diagnose_shell_failure(command: str, cwd: str, exit_code: int, output: str,
                            *, output_source: str, parse_command,
                            env: dict[str, str] | None = None):
    candidate = _shell_failure_candidate(command, cwd, exit_code, output, parse_command, env)
    if candidate is None:
        return None
    (argv, effective_cwd, command_env), reported_path = candidate
    merged_env = dict(env or {})
    merged_env.update(command_env)
    deadline = time.monotonic() + DIAGNOSIS_BUDGET_SECONDS
    payload = {
        "argv": argv, "cwd": effective_cwd, "returncode": exit_code,
        "reported_path": reported_path, "env": merged_env,
        "deadline": deadline, "output_source": output_source,
    }
    return _bounded_worker(payload, deadline)


def diagnose_codex_command(command: str, cwd: str, exit_code: int,
                           aggregated_output: str, *, env: dict[str, str] | None = None) -> dict | None:
    """Consume a structured App Server result without inventing output streams."""
    return _diagnose_shell_failure(
        command, cwd, exit_code, aggregated_output,
        output_source="Codex App Server aggregatedOutput",
        parse_command=_codex_command_argv, env=env,
    )


def diagnose_opencode_command(command: str, cwd: str, exit_code: int,
                              combined_output: str) -> dict | None:
    """Consume OpenCode's native exit plus combined model-facing shell output."""
    return _diagnose_shell_failure(
        command, cwd, exit_code, combined_output,
        output_source="OpenCode combined output",
        parse_command=parse_shell_command,
    )


def _text_bytes(value, encoding: str) -> tuple[bytes, bool]:
    if isinstance(value, bytes):
        return value, True
    if isinstance(value, str):
        return value.encode(encoding, errors="replace"), False
    return b"", False


def _response_code(response: dict) -> int | None:
    for key in _RESPONSE_CODES:
        value = response.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _hook_fields(payload: dict, harness: str):
    if harness in {"codex", "claude"}:
        event = payload.get("hook_event_name")
        allowed = {"PostToolUse"} if harness == "codex" else {"PostToolUse", "PostToolUseFailure"}
        if event not in allowed:
            return None
        name, inputs, response, cwd = (
            payload.get("tool_name"), payload.get("tool_input"),
            payload.get("tool_response"), payload.get("cwd"))
        if (harness == "codex" and isinstance(inputs, dict)
                and name in {"exec_command", "functions.exec_command"}):
            command = inputs.get("cmd")
            supplied_cwd = inputs.get("workdir")
            if isinstance(supplied_cwd, str) and supplied_cwd:
                if os.path.isabs(supplied_cwd):
                    cwd = supplied_cwd
                elif isinstance(cwd, str) and cwd:
                    cwd = os.path.join(cwd, supplied_cwd)
                else:
                    cwd = ""
        else:
            command = inputs.get("command") if isinstance(inputs, dict) else None
        if harness == "claude" and not isinstance(response, dict):
            response = payload.get("tool_result")
        if harness == "claude" and event == "PostToolUseFailure":
            if not isinstance(name, str) or name.lower() not in {"bash", "shell"}:
                return None
            error = payload.get("error")
            if not isinstance(error, str):
                return None
            match = re.match(r"^Exit code (-?[0-9]+)(?:\r?\n|$)", error)
            if not match:
                return None
            try:
                code = int(match.group(1))
            except ValueError:
                return None
            return (
                inputs.get("command") if isinstance(inputs, dict) else None,
                cwd, code, b"", error.encode(locale.getpreferredencoding(False) or "utf-8",
                                              errors="replace"), False)
    else:
        return None
    if not isinstance(name, str) or name.lower() not in {
        "bash", "shell", "exec_command", "functions.exec_command"
    }:
        return None
    if not isinstance(inputs, dict):
        return None
    if not isinstance(command, str) or not isinstance(cwd, str) or not cwd:
        return None
    if not isinstance(response, dict):
        return None
    code = _response_code(response)
    if code is None or code == 0:
        return None
    encoding = locale.getpreferredencoding(False) or "utf-8"
    stdout, stdout_raw = _text_bytes(response.get("stdout", ""), encoding)
    stderr, stderr_raw = _text_bytes(response.get("stderr", ""), encoding)
    raw_available = bool(response.get("raw_bytes_available")) and stdout_raw and stderr_raw
    return command, cwd, code, stdout, stderr, bool(raw_available)


def diagnose_hook_payload(payload: dict, harness: str) -> dict | None:
    if harness == "opencode":
        name = payload.get("tool_name")
        inputs = payload.get("tool_input")
        response = payload.get("tool_response")
        cwd = payload.get("cwd")
        if (not isinstance(name, str) or name.lower() != "bash"
                or not isinstance(inputs, dict) or not isinstance(response, dict)):
            return None
        return diagnose_opencode_command(
            inputs.get("command"), cwd, response.get("exit"), response.get("output"),
        )
    fields = _hook_fields(payload, harness)
    if fields is None:
        return None
    command, cwd, code, stdout, stderr, raw_available = fields
    parsed = parse_shell_command(command, cwd)
    unparsed_output = parsed is None
    if unparsed_output:
        parsed = _fallback_worktree_context(cwd)
    if parsed is None:
        return None
    argv, effective_cwd, extra_env = parsed
    diagnosis = diagnose_failure(
        argv, effective_cwd, code, stdout, stderr, env=extra_env,
        output_source="unparsed wrapper output" if unparsed_output else "Git stderr",
    )
    if diagnosis is not None:
        diagnosis["raw_bytes_available"] = raw_available
    return diagnosis


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--harness", choices=("codex", "claude", "opencode"))
    parser.add_argument("--diagnose-worker", action="store_true")
    args = parser.parse_args()
    try:
        payload = json.load(sys.stdin)
        if args.diagnose_worker:
            result = _diagnose_payload(payload) if isinstance(payload, dict) else None
            sys.stdout.write(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
            return 0
        result = diagnose_hook_payload(payload, args.harness or "") if isinstance(payload, dict) else None
        print(json.dumps({"diagnosis": result}, ensure_ascii=False) if result and result["state"] != "unbound" else "{}")
    except (OSError, ValueError, TypeError, UnicodeError):
        print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
