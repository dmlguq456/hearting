#!/usr/bin/env python3
"""Opt-in real Codex native parent/continuation replay in isolated runtime state.

Usage: python3 -B utilities/codex_parent_completion_replay.py <output-dir>

The single argument is an evidence directory. The command creates an ephemeral
Codex home, worktree, registry, delivery record, and thread in a private runtime
temporary directory separate from the project checkout, then removes it. It never resumes an
existing user thread. Credentials are copied only into the private temporary
home, removed from that home after app-server initialization, and never written
to the evidence directory.
"""
from __future__ import annotations

import argparse
import datetime
import glob
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import tomllib
import time
import uuid
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
UTILITIES = ROOT / "utilities"
if str(UTILITIES) not in sys.path:
    sys.path.insert(0, str(UTILITIES))

import codex_queue_delivery
import dispatch_completion_join as completion_join
import dispatch_pending_delivery as pending_delivery
import dispatch_session_sweep
import dispatch_supervision
from detached_lifetime import bind as end_with_test


class ReplayBlocked(RuntimeError):
    pass


class ReplayFailed(RuntimeError):
    pass


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def _append_event(path: Path, event: dict) -> None:
    event = {"observed_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z"), **event}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")


def _copy_regular(source: Path, target: Path, *, private: bool = False) -> bool:
    try:
        info = source.lstat()
        if not stat.S_ISREG(info.st_mode):
            return False
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        os.chmod(target, 0o600 if private else stat.S_IMODE(info.st_mode) & 0o600 or 0o600)
        return True
    except OSError:
        return False


def _copy_runtime_config(source_home: Path, codex_home: Path, env: dict[str, str]
                        ) -> tuple[dict[str, bool], Path | None]:
    copied: dict[str, bool] = {"config.toml": True, "hooks.json": False, "installation_id": False}
    template = json.loads((ROOT / "adapters/codex/hooks/hooks.json").read_text(encoding="utf-8"))
    all_hooks = template.get("hooks") if isinstance(template, dict) else None
    selected = {}
    for event, script_name in (("SessionStart", "sessionstart-lifecycle.py"),
                               ("UserPromptSubmit", "userprompt-lifecycle.py")):
        entries = all_hooks.get(event) if isinstance(all_hooks, dict) else None
        matched = []
        for entry in entries if isinstance(entries, list) else []:
            hooks = entry.get("hooks") if isinstance(entry, dict) else None
            keep = [hook for hook in hooks if isinstance(hook, dict)
                    and script_name in str(hook.get("command", ""))] if isinstance(hooks, list) else []
            if keep:
                matched.append({**entry, "hooks": keep})
        if not matched:
            raise ReplayBlocked("native-hook-fixture-template-incomplete:" + event)
        selected[event] = matched
    # Only the two lifecycle paths under test are enabled in this private home.
    (codex_home / "hooks.json").write_text(
        json.dumps({"hooks": selected}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (codex_home / "hooks.json").chmod(0o600)
    copied["hooks.json"] = True
    # Personal MCP, plugin, provider, and shell configuration is deliberately
    # absent. Native integrations and multi-agent dispatch stay disabled.
    (codex_home / "config.toml").write_text(
        "[analytics]\nenabled = false\n\n[features]\napps = false\nmulti_agent = false\n",
        encoding="utf-8")
    (codex_home / "config.toml").chmod(0o600)
    (codex_home / "installation_id").write_text(uuid.uuid4().hex + "\n", encoding="ascii")
    (codex_home / "installation_id").chmod(0o600)
    copied["installation_id"] = True
    copied["auth.json"] = False
    auth_source = None
    for source in (source_home / "auth.json", Path.home() / ".codex" / "auth.json"):
        if _copy_regular(source, codex_home / "auth.json", private=True):
            copied["auth.json"] = True
            auth_source = source
            break
    return copied, auth_source


def _event_key(value: str) -> str:
    return "".join(character.lower() for character in value if character.isalnum())


def _same_source(raw: object, expected: Path) -> bool:
    if not isinstance(raw, str) or not raw:
        return False
    candidate = Path(raw).expanduser()
    try:
        return candidate.samefile(expected)
    except OSError:
        return candidate.absolute() == expected.absolute()


def _listed_isolated_hooks(client, worktree: Path, hooks_path: Path) -> dict[str, dict]:
    response = client.request("hooks/list", {"cwds": [str(worktree)]})
    rows = response.get("data") if isinstance(response, dict) else None
    if not isinstance(rows, list):
        raise ReplayBlocked("isolated-hook-list-shape-invalid")
    hooks = []
    for row in rows:
        if not isinstance(row, dict) or row.get("cwd") != str(worktree):
            continue
        entries = row.get("hooks")
        if not isinstance(entries, list):
            raise ReplayBlocked("isolated-hook-list-entry-invalid")
        hooks.extend(hook for hook in entries if isinstance(hook, dict)
                     and _same_source(hook.get("sourcePath"), hooks_path))

    try:
        source = json.loads(hooks_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplayBlocked("isolated-hook-source-unreadable") from exc
    definitions = source.get("hooks") if isinstance(source, dict) else None
    expected_events: dict[str, int] = {}
    if isinstance(definitions, dict):
        for event, groups in definitions.items():
            if not isinstance(event, str) or not isinstance(groups, list):
                raise ReplayBlocked("isolated-hook-source-shape-invalid")
            for group in groups:
                if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                    raise ReplayBlocked("isolated-hook-source-entry-invalid")
                count = sum(isinstance(item, dict) for item in group["hooks"])
                if count:
                    key = _event_key(event)
                    expected_events[key] = expected_events.get(key, 0) + count
    observed_events: dict[str, int] = {}
    by_key = {}
    for hook in hooks:
        key = hook.get("key")
        current_hash = hook.get("currentHash")
        event = hook.get("eventName")
        if (not isinstance(key, str) or not key or key in by_key
                or not isinstance(current_hash, str) or not current_hash
                or not isinstance(event, str) or not event):
            raise ReplayBlocked("isolated-hook-trust-identity-invalid")
        if hook.get("enabled") is False:
            raise ReplayBlocked("isolated-hook-disabled:" + _event_key(event))
        by_key[key] = hook
        event_key = _event_key(event)
        observed_events[event_key] = observed_events.get(event_key, 0) + 1
    if not by_key or observed_events != expected_events:
        raise ReplayBlocked("isolated-hook-source-set-mismatch")
    return by_key


def _trust_isolated_hooks(client, codex_home: Path, worktree: Path,
                          hooks_path: Path) -> dict:
    """Trust only the generated hooks through Codex's current-hash config API."""
    config_path = codex_home / "config.toml"
    home_info = codex_home.lstat()
    if (not stat.S_ISDIR(home_info.st_mode) or home_info.st_uid != os.geteuid()
            or stat.S_IMODE(home_info.st_mode) != 0o700):
        raise ReplayBlocked("isolated-codex-home-ownership-unsafe")
    for path in (config_path, hooks_path):
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or path.parent.resolve(strict=True) != codex_home.resolve(strict=True)):
            raise ReplayBlocked("isolated-hook-config-ownership-unsafe")
    hook_bytes = hooks_path.read_bytes()
    hook_digest = hashlib.sha256(hook_bytes).hexdigest()
    try:
        config_data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ReplayBlocked("isolated-hook-config-invalid") from exc
    existing_state = config_data.get("hooks", {}).get("state", {})
    if existing_state:
        raise ReplayBlocked("isolated-hook-state-not-private-empty")

    listed = _listed_isolated_hooks(client, worktree, hooks_path)
    expected_hashes = {key: hook["currentHash"] for key, hook in listed.items()}
    edits = [{"keyPath": "hooks.state", "value": {key: {"trusted_hash": current_hash}},
              "mergeStrategy": "upsert"}
             for key, current_hash in expected_hashes.items()]
    mode = stat.S_IMODE(config_path.stat().st_mode)
    client.request("config/batchWrite", {
        "edits": edits, "reloadUserConfig": True, "filePath": str(config_path),
    })
    os.chmod(config_path, mode)
    if hooks_path.read_bytes() != hook_bytes:
        raise ReplayBlocked("isolated-hook-source-changed-during-trust")
    try:
        trusted_state = tomllib.loads(config_path.read_text(encoding="utf-8"))
        trusted_state = trusted_state.get("hooks", {}).get("state", {})
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ReplayBlocked("isolated-hook-trust-config-invalid") from exc
    if (not isinstance(trusted_state, dict) or set(trusted_state) != set(expected_hashes)
            or any(not isinstance(trusted_state.get(key), dict)
                   or trusted_state[key].get("trusted_hash") != digest
                   for key, digest in expected_hashes.items())):
        raise ReplayBlocked("isolated-hook-trust-hash-not-persisted")

    verified = _listed_isolated_hooks(client, worktree, hooks_path)
    if (set(verified) != set(expected_hashes)
            or any(verified[key].get("currentHash") != digest
                   or verified[key].get("trustStatus") not in {"trusted", "managed"}
                   for key, digest in expected_hashes.items())):
        raise ReplayBlocked("isolated-hook-current-hash-not-trusted")
    if hooks_path.read_bytes() != hook_bytes:
        raise ReplayBlocked("isolated-hook-source-changed-after-trust")
    return {"hooks_json_sha256": hook_digest, "trusted_hook_count": len(expected_hashes),
            "trusted_hook_keys": sorted(expected_hashes)}


def _start_replay_thread(client, worktree: Path) -> str:
    result = client.request("thread/start", {
        "cwd": str(worktree), "ephemeral": False, "sandbox": "workspace-write",
        "approvalPolicy": "never",
    })
    thread = result.get("thread") if isinstance(result, dict) else None
    thread_id = thread.get("id") if isinstance(thread, dict) else None
    if not isinstance(thread_id, str) or not thread_id:
        raise ReplayBlocked("isolated-thread-start-unconfirmed")
    return thread_id


def _start_trusted_replay_thread(client, codex_home: Path, worktree: Path,
                                 hooks_path: Path) -> tuple[str, str, dict]:
    probe_thread_id = _start_replay_thread(client, worktree)
    hook_trust = _trust_isolated_hooks(client, codex_home, worktree, hooks_path)
    client.request("thread/unsubscribe", {"threadId": probe_thread_id})
    thread_id = _start_replay_thread(client, worktree)
    return thread_id, probe_thread_id, hook_trust


def _fake_agent_home(temp_root: Path) -> Path:
    """Give hook path resolution a private legacy state root."""
    home = temp_root / "agent-home"
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    (home / "core").mkdir(mode=0o700, exist_ok=True)
    (home / "core" / "CORE.md").write_text("Isolated Codex parent replay.\n", encoding="utf-8")
    for name in ("adapters", "capabilities", "roles", "utilities", "hooks", "tools", "config"):
        link = home / name
        if not link.exists():
            link.symlink_to(ROOT / name, target_is_directory=True)
    return home


def _stderr_summary(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "stderr-unavailable"
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if not lines:
        return "stderr-empty"
    import re
    text = " ".join(lines[-3:])
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~-]+", r"\1<redacted>", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "<redacted-api-key>", text)
    return text[:320]


def _start_server(command: list[str], endpoint: Path, worktree: Path, env: dict[str, str],
                  stderr_path: Path):
    endpoint.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_dirs = (endpoint.parent, endpoint.parent.parent, endpoint.parent.parent.parent)
    for directory in private_dirs:
        os.chmod(directory, 0o700)
    parent_info = endpoint.parent.stat()
    home_info = endpoint.parent.parent.stat()
    if (any(info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700
            for info in (parent_info, home_info))):
        raise ReplayBlocked("isolated-app-server-socket-directory-unsafe")
    if endpoint.exists():
        endpoint.unlink()
    stderr_file = stderr_path.open("ab", buffering=0)
    try:
        process = subprocess.Popen(
            command,
            cwd=worktree, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=stderr_file,
            close_fds=True, start_new_session=True,
        )
    except OSError:
        stderr_file.close()
        raise
    end_with_test(process)
    process._replay_stderr_file = stderr_file
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            summary = _stderr_summary(stderr_path)
            stderr_file.close()
            raise ReplayBlocked("isolated-codex-app-server-exited-before-listen:"
                                + summary + f"; socket_dir_mode={oct(stat.S_IMODE(parent_info.st_mode))}"
                                + f"; codex_home_mode={oct(stat.S_IMODE(home_info.st_mode))}")
        try:
            info = codex_queue_delivery.resolve_endpoint(endpoint).lstat()
            if stat.S_ISSOCK(info.st_mode) and info.st_uid == os.geteuid():
                return process
        except (FileNotFoundError, codex_queue_delivery.QueueDeliveryError):
            pass
        time.sleep(0.05)
    summary = _stderr_summary(stderr_path)
    _stop_server(process)
    raise ReplayBlocked("isolated-codex-app-server-listen-timeout:" + summary)


def _stop_server(process) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    stderr_file = getattr(process, "_replay_stderr_file", None)
    if stderr_file is not None:
        stderr_file.close()


def _rpc_session(endpoint: Path):
    socket_client = codex_queue_delivery._WebSocket(endpoint, timeout=10)
    try:
        socket_client.request("initialize", {
            "clientInfo": {"name": "hearting-parent-replay", "title": "Hearting Parent Replay",
                           "version": "1"},
            "capabilities": None,
        })
        socket_client._send_frame(0x1, json.dumps(
            {"jsonrpc": "2.0", "method": "initialized"}, separators=(",", ":")
        ).encode("utf-8"))
        return socket_client
    except Exception:
        socket_client.close()
        raise


def _request(endpoint: Path, method: str, params: dict) -> dict:
    client = _rpc_session(endpoint)
    try:
        return client.request(method, params)
    finally:
        client.close()


def _turn_record(endpoint: Path, thread_id: str, turn_id: str) -> dict | None:
    cursor = None
    for _ in range(codex_queue_delivery.MAX_QUEUE_PAGES):
        params = {"threadId": thread_id, "limit": codex_queue_delivery.MAX_PAGE_SIZE,
                  "sortDirection": "desc", "itemsView": "full"}
        if cursor:
            params["cursor"] = cursor
        response = codex_queue_delivery._rpc(endpoint, "thread/turns/list", params, timeout=10)
        turns = response.get("data")
        if not isinstance(turns, list):
            raise ReplayBlocked("native-turn-history-unavailable")
        for turn in turns:
            if isinstance(turn, dict) and turn.get("id") == turn_id:
                return turn
        cursor = response.get("nextCursor")
        if not cursor:
            return None
    raise ReplayBlocked("native-turn-history-page-limit")


def _all_turns(endpoint: Path, thread_id: str) -> list[dict]:
    cursor = None
    found = []
    for _ in range(codex_queue_delivery.MAX_QUEUE_PAGES):
        params = {"threadId": thread_id, "limit": codex_queue_delivery.MAX_PAGE_SIZE,
                  "sortDirection": "desc", "itemsView": "full"}
        if cursor:
            params["cursor"] = cursor
        response = codex_queue_delivery._rpc(endpoint, "thread/turns/list", params, timeout=10)
        page = response.get("data")
        if not isinstance(page, list):
            raise ReplayBlocked("native-turn-history-unavailable")
        found.extend(item for item in page if isinstance(item, dict))
        cursor = response.get("nextCursor")
        if not cursor:
            return found
    raise ReplayBlocked("native-turn-history-page-limit")


def _turn_for_client_message(turns: list[dict], client_id: str) -> dict | None:
    matches = []
    for turn in turns:
        items = turn.get("items", []) if isinstance(turn.get("items"), list) else []
        if any(isinstance(item, dict) and item.get("clientId") == client_id for item in items):
            matches.append(turn)
    if len(matches) > 1:
        raise ReplayBlocked("continuation-client-id-history-conflict")
    return matches[0] if matches else None


def _turn_items(turn: dict) -> tuple[list[str], list[dict]]:
    messages, tools = [], []
    for item in turn.get("items", []) if isinstance(turn.get("items"), list) else []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
            messages.append(item["text"])
        elif item.get("type") == "commandExecution":
            tools.append({key: item.get(key) for key in
                          ("id", "command", "commandActions", "cwd", "aggregatedOutput",
                           "exitCode", "status") if key in item})
    return messages, tools


def _successful_tool(item: dict) -> bool:
    return item.get("status") in {"completed", "success"} and item.get("exitCode") == 0


def _file_snapshot(path: Path) -> tuple[bytes | None, int | None]:
    try:
        info = path.stat()
        return path.read_bytes(), info.st_mtime_ns
    except OSError:
        return None, None


def _file_hash_matches(path: Path, expected_hash: str) -> bool:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest() == expected_hash
    except OSError:
        return False


def _same_file(raw: str, expected: Path, cwd: Path | None) -> bool:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        if cwd is None:
            return False
        candidate = cwd / candidate
    try:
        return candidate.samefile(expected)
    except OSError:
        return False


def _shell_segments(command: str) -> list[list[str]]:
    """Split a recorded shell command for inspection without executing it."""
    try:
        wrapper = shlex.split(command)
    except ValueError:
        return []
    if wrapper and Path(wrapper[0]).name in {"sh", "bash", "zsh"}:
        for index, token in enumerate(wrapper[:-1]):
            if token in {"-c", "-lc", "-ec"} or (token.startswith("-") and "c" in token[1:]):
                return _shell_segments(wrapper[index + 1])

    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return []
    segments, current = [], []
    for token in tokens:
        if token and all(character in ";&|" for character in token):
            if current:
                segments.append(current)
                current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def _single_shell_command(command: str) -> bool:
    """Whether a native result can be attributed to one exact shell command."""
    try:
        wrapper = shlex.split(command)
    except ValueError:
        return False
    if wrapper and Path(wrapper[0]).name in {"sh", "bash", "zsh"}:
        for index, token in enumerate(wrapper[:-1]):
            if token in {"-c", "-lc", "-ec"} or (token.startswith("-") and "c" in token[1:]):
                return _single_shell_command(wrapper[index + 1])
    if "\n" in command or "\r" in command:
        return False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|><()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return False
    operators = [token for token in tokens
                 if token and all(character in ";&|><()" for character in token)]
    shell_flow = {"if", "then", "else", "elif", "fi", "while", "until", "for", "do",
                  "done", "case", "esac", "function"}
    return (not operators and not shell_flow.intersection(tokens)
            and len(_shell_segments(command)) == 1)


def _tool_commands(item: dict) -> list[tuple[str, str | None, str, dict | None]]:
    actions = item.get("commandActions")
    commands = []
    if isinstance(actions, list):
        for action in actions:
            if isinstance(action, dict) and isinstance(action.get("command"), str):
                commands.append((action["command"], action.get("cwd")
                                 if isinstance(action.get("cwd"), str) else None,
                                 "commandAction", action))
    if not commands and isinstance(item.get("command"), str):
        commands.append((item["command"], None, "commandExecution", None))
    # Keep repeated action entries distinct: two invocations with the same
    # spelling are still two attempts and must fail the one-time check.
    return commands


def _command_contexts(item: dict):
    enclosing_command = item.get("command") if isinstance(item.get("command"), str) else ""
    enclosing_segments = _shell_segments(enclosing_command) if enclosing_command else []
    execution_cwd_value = item.get("cwd") if isinstance(item.get("cwd"), str) else None
    execution_cwd = Path(execution_cwd_value).expanduser() if execution_cwd_value else None
    enclosing_result_is_exact = (bool(enclosing_command)
                                 and _single_shell_command(enclosing_command)
                                 and len(enclosing_segments) == 1)
    for command, action_cwd, source, action in _tool_commands(item):
        cwd_value = action_cwd or (item.get("cwd") if isinstance(item.get("cwd"), str) else None)
        cwd = Path(cwd_value).expanduser() if cwd_value else None
        if cwd is not None and (not cwd.is_absolute() or not cwd.is_dir()):
            cwd = None
        segments = _shell_segments(command)
        action_cwd_matches_envelope = True
        if source == "commandAction" and action_cwd:
            action_path = Path(action_cwd).expanduser()
            try:
                action_cwd_matches_envelope = (execution_cwd is not None
                                               and action_path.samefile(execution_cwd))
            except OSError:
                action_cwd_matches_envelope = (execution_cwd is not None
                                               and action_path.absolute() == execution_cwd.absolute())
        command_matches_envelope = (source == "commandExecution"
                                    or (len(segments) == 1 and bool(enclosing_segments)
                                        and segments[0] in enclosing_segments))
        action_has_result = (
            source == "commandAction" and isinstance(action, dict)
            and isinstance(action.get("status"), str)
            and type(action.get("exitCode")) is int
            and action.get("status") in {"completed", "success", "failed", "error"}
        )
        action_result_proven = (
            action_has_result and bool(enclosing_command) and command_matches_envelope
            and action_cwd_matches_envelope)
        envelope_result_proven = (
            enclosing_result_is_exact and _single_shell_command(command)
            and command_matches_envelope and action_cwd_matches_envelope)
        result_proven = action_result_proven or envelope_result_proven
        if action_result_proven:
            execution_succeeded = (action.get("status") in {"completed", "success"}
                                   and action.get("exitCode") == 0)
            action_output = action.get("aggregatedOutput", action.get("output"))
            execution_output = (action_output if isinstance(action_output, str)
                                else str(item.get("aggregatedOutput", ""))
                                if envelope_result_proven else "")
            result_reason = None if execution_succeeded else "command-action-exit-nonzero"
        elif envelope_result_proven:
            execution_succeeded = _successful_tool(item)
            execution_output = str(item.get("aggregatedOutput", ""))
            result_reason = None if execution_succeeded else "command-execution-failed"
        elif not enclosing_command:
            execution_succeeded = False
            execution_output = ""
            result_reason = "missing-command-execution-envelope"
        elif not enclosing_result_is_exact:
            execution_succeeded = False
            execution_output = ""
            result_reason = "ambiguous-shell-compound-no-per-command-result"
        elif not action_cwd_matches_envelope:
            execution_succeeded = False
            execution_output = ""
            result_reason = "command-action-cwd-does-not-match-execution-envelope"
        else:
            execution_succeeded = False
            execution_output = ""
            result_reason = "command-action-does-not-match-execution-envelope"
        for segment in segments:
            yield (segment, cwd, "commandAction" if action_cwd else "commandExecution",
                   result_proven, execution_succeeded, result_reason, execution_output)


def _resolved_arguments(tokens: list[str], cwd: Path | None) -> list[Path]:
    resolved = []
    for token in tokens:
        if not token or token.startswith("-") or any(char in token for char in "$`\n"):
            continue
        candidate = Path(token).expanduser()
        if not candidate.is_absolute():
            if cwd is None:
                continue
            candidate = cwd / candidate
        matches = glob.glob(str(candidate), recursive=False) if glob.has_magic(str(candidate)) else [str(candidate)]
        for match in matches:
            path = Path(match)
            try:
                if path.is_file():
                    resolved.append(path)
            except OSError:
                continue
    return resolved


def _read_evidence(item: dict, expected: Path) -> dict | None:
    if not _successful_tool(item):
        return None
    try:
        exact_content = expected.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for (tokens, cwd, cwd_source, result_proven, execution_succeeded,
         _result_reason, output) in _command_contexts(item):
        if not result_proven or not execution_succeeded:
            continue
        if not tokens or Path(tokens[0]).name != "cat":
            continue
        matched = [path for path in _resolved_arguments(tokens[1:], cwd)
                   if _same_file(str(path), expected, None)]
        if not matched or exact_content not in output:
            continue
        return {"tool_id": item.get("id"), "cwd": str(cwd) if cwd else None,
                "cwd_source": cwd_source, "command": " ".join(tokens),
                "matched_file": str(expected), "exact_content_observed": True}
    return None


def _script_invocations(tools: list[dict], expected: Path,
                        output_marker: str | None = None) -> list[dict]:
    invocations = []
    for item in tools:
        for (tokens, cwd, cwd_source, result_proven, execution_succeeded,
             result_reason, output) in _command_contexts(item):
            if not tokens:
                continue
            executable = Path(tokens[0]).name.lower()
            if executable.startswith("python"):
                index = 1
                script = None
                while index < len(tokens):
                    token = tokens[index]
                    if token in {"-c", "-m"} or token.startswith(("-c", "-m")):
                        break
                    if token == "--":
                        script = tokens[index + 1] if index + 1 < len(tokens) else None
                        break
                    if token in {"-W", "-X"}:
                        index += 2
                        continue
                    if token.startswith("-"):
                        index += 1
                        continue
                    script = token
                    break
                matches = [script] if script and _same_file(script, expected, cwd) else []
            elif _same_file(tokens[0], expected, cwd):
                matches = [tokens[0]]
            else:
                matches = []
            for matched in matches:
                invocations.append({"tool_id": item.get("id"), "cwd": str(cwd) if cwd else None,
                                    "cwd_source": cwd_source, "command": " ".join(tokens),
                                    "matched_script": str(expected),
                                    "tool_succeeded": result_proven and execution_succeeded,
                                    "execution_result_proven": result_proven,
                                    "execution_result_reason": result_reason,
                                    "output_marker_observed": output_marker is None
                                    or output_marker in output})
    return invocations


def _write_native_fixture(worktree: Path, sentinel: str) -> dict:
    """Build the fully labelled, synthetic chronology consumed by the native turn."""
    fixture = worktree / ".agent_reports" / "native-parent-fixture"
    fixture.mkdir(mode=0o700, parents=True, exist_ok=True)
    fixture_id = "fixture-" + hashlib.sha256(sentinel.encode()).hexdigest()[:12]
    events = [
        {"sequence": 1, "event": "original-code-attempt", "result": "FAIL",
         "evidence": "synthetic-request-boundary-assertion", "reason": "The original request boundary rejected the fixture input."},
        {"sequence": 2, "event": "fix-applied", "result": "FIXED",
         "evidence": "synthetic-parser-correction", "reason": "The fixture parser now accepts the documented input."},
        {"sequence": 3, "event": "verification", "result": "PASS",
         "evidence": "fixture-verifier-6-checks", "reason": "Six isolated fixture assertions passed."},
        {"sequence": 4, "event": "deployment", "result": "SUCCESS",
         "evidence": "synthetic-deployment", "reason": "A synthetic deployment record was marked successful."},
        {"sequence": 5, "event": "independent-review", "result": "PASS",
         "evidence": "synthetic-review-01", "reason": "The isolated fixture review record passed."},
        {"sequence": 6, "event": "documentation", "result": "COMPLETE",
         "evidence": "synthetic-docs-complete", "reason": "The isolated fixture documentation record is complete."},
    ]
    synthetic = {
        "commit": f"synthetic-only:commit-id=fixture-commit-{fixture_id}",
        "deployment": f"synthetic-only:status=success id=fixture-deploy-{fixture_id}",
        "serving": f"synthetic-only:synthetic://serving/{fixture_id} (not probed)",
    }
    skipped = [
        "Production deployment and serving checks were skipped; the displayed values are synthetic.",
        "The historical direct dispatch_completion_join suite recorded 2 skips; this native replay did not rerun it.",
    ]
    limits = [
        "All six chronology records and the deployment, review, commit, and serving values are synthetic fixture data.",
        "No production endpoint, deployment, commit, CI run, PR, or operating state was changed or measured.",
        "This isolated app-server replay tests Codex delivery and reporting only.",
    ]
    report = {
        "fixture": "synthetic-isolated-codex-parent-replay",
        "fixture_id": fixture_id,
        "reason": "Complete the already approved isolated parent follow-through after reading the exact result.",
        "before": {"followthrough_file": "absent", "approved_action_count": 0},
        "after": {"followthrough_file": "present", "approved_action_count": 1,
                    "sentinel": sentinel},
        "chronology": events,
        "verification": {"command": "python3 -B verify_fixture.py", "result": "PASS",
                          "checks_passed": 6, "checks_failed": 0},
        "skipped": skipped,
        "synthetic_values": synthetic,
        "limits": limits,
    }
    facts = {"fixture_id": fixture_id, "sentinel": sentinel, "events": events,
             "report": report, "skipped": skipped, "limits": limits,
             "synthetic_values": synthetic}
    facts_path = fixture / "fixture-facts.json"
    _write_json(facts_path, facts)
    artifact_paths = []
    for event in events:
        path = fixture / f"{event['sequence']:02d}-{event['event']}.md"
        path.write_text(
            f"FIXTURE_ID={fixture_id}\nEVENT_ID={event['evidence']}\n"
            f"SEQUENCE={event['sequence']}\nEVENT={event['event']}\nRESULT={event['result']}\n"
            f"REASON={event['reason']}\nSYNTHETIC=true\n",
            encoding="utf-8")
        artifact_paths.append(path)
    verifier = fixture / "verify_fixture.py"
    verifier.write_text(
        "import json\nfrom pathlib import Path\n"
        "root = Path(__file__).resolve().parent\n"
        "facts = json.loads((root / 'fixture-facts.json').read_text(encoding='utf-8'))\n"
        "events = facts['events']\n"
        "assert [item['sequence'] for item in events] == list(range(1, 7))\n"
        "assert [item['result'] for item in events] == ['FAIL', 'FIXED', 'PASS', 'SUCCESS', 'PASS', 'COMPLETE']\n"
        "for item in events:\n"
        "    path = root / f\"{item['sequence']:02d}-{item['event']}.md\"\n"
        "    text = path.read_text(encoding='utf-8')\n"
        "    assert f\"EVENT_ID={item['evidence']}\" in text\n"
        "    assert f\"RESULT={item['result']}\" in text\n"
        "    assert 'SYNTHETIC=true' in text\n"
        "print('FIXTURE_VERIFICATION=PASS checks=6 failed=0')\n",
        encoding="utf-8")
    followthrough = fixture / "follow-through.json"
    action = fixture / "apply_fixture_followthrough.py"
    action.write_text(
        "import json, sys\nfrom pathlib import Path\n"
        "root = Path(__file__).resolve().parent\n"
        "facts = json.loads((root / 'fixture-facts.json').read_text(encoding='utf-8'))\n"
        "sentinel = sys.argv[1]\n"
        "if sentinel != facts['sentinel']:\n    raise SystemExit('fixture-sentinel-mismatch')\n"
        "target = root / 'follow-through.json'\n"
        "try:\n"
        "    with target.open('x', encoding='utf-8') as stream:\n"
        "        json.dump({'fixture_id': facts['fixture_id'], 'sentinel': sentinel,\n"
        "                   'approved_action_count': 1, 'synthetic': True}, stream, sort_keys=True)\n"
        "        stream.write('\\n')\n"
        "except FileExistsError:\n    raise SystemExit('fixture-followthrough-already-applied')\n"
        "print('FOLLOWTHROUGH_APPLIED=' + facts['fixture_id'])\n",
        encoding="utf-8")
    return {"fixture": fixture, "facts": facts, "facts_path": facts_path,
            "artifact_paths": artifact_paths, "verifier": verifier,
            "verifier_sha256": hashlib.sha256(verifier.read_bytes()).hexdigest(),
            "action": action, "action_sha256": hashlib.sha256(action.read_bytes()).hexdigest(),
            "followthrough": followthrough}


def _factual_report_object(text: str) -> dict | None:
    marker = "FACTUAL_REPORT_JSON:"
    offset = text.find(marker)
    if offset < 0:
        return None
    raw = text[offset + len(marker):].lstrip()
    if raw.startswith("```"):
        fenced = re.fullmatch(
            r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n```[ \t\r\n]*",
            raw, flags=re.IGNORECASE | re.DOTALL)
        if fenced is None:
            return None
        raw = fenced.group(1).lstrip(" \t\r\n")
    try:
        value, end = json.JSONDecoder().raw_decode(raw)
    except (TypeError, ValueError):
        return None
    if raw[end:].strip():
        return None
    return value if isinstance(value, dict) else None


def _raw_prose_sentences(prose: str) -> list[str]:
    return [part.strip() for part in
            re.split(r"(?<=[.!?。！？])\s+|[\r\n]+", prose) if part.strip()]


def _prose_sentences(prose: str) -> list[str]:
    return [part.casefold() for part in _raw_prose_sentences(prose)]


def _prose_windows(prose: str, *, max_sentences: int = 2) -> list[str]:
    windows = []
    for paragraph in re.split(r"\n\s*\n", prose):
        sentences = _prose_sentences(paragraph)
        for start in range(len(sentences)):
            for end in range(start + 1, min(len(sentences), start + max_sentences) + 1):
                windows.append(" ".join(sentences[start:end]))
    return windows


def _has_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _label_sections(prose: str, direction: str) -> list[str]:
    labels = {
        "before": (r"(?<![A-Za-z0-9])(?:before|earlier|previously|prior\s+to|initially)\b"
                   r"|(?:이전(?:에는|에|의)?|전에는|처음에는)"),
        "after": (r"(?<![A-Za-z0-9])(?:after(?:ward)?|subsequently|now)\b"
                  r"|(?:이후(?:에는|에)?|후에는|그\s*뒤(?:에는|에)?)"),
    }
    pattern = re.compile(
        rf"(?P<before>{labels['before']})|(?P<after>{labels['after']})",
        flags=re.IGNORECASE)
    sections = []
    for paragraph in re.split(r"\n\s*\n", prose):
        matches = list(pattern.finditer(paragraph))
        for index, match in enumerate(matches):
            if match.lastgroup != direction:
                continue
            end = matches[index + 1].start() if index + 1 < len(matches) else len(paragraph)
            sentences = _raw_prose_sentences(paragraph[match.end():end])
            if sentences:
                sections.append(" ".join(sentences[:2]))
    return sections


def _expected_identifier(field: str, value: str) -> str | None:
    patterns = {
        "commit": r"commit-id=([A-Za-z0-9][A-Za-z0-9._-]*)",
        "deployment": r"\bid=([A-Za-z0-9][A-Za-z0-9._-]*)",
        "serving": r"(synthetic://[^\s)`,;]+)",
    }
    match = re.search(patterns[field], value, flags=re.IGNORECASE)
    return match.group(1) if match else None


def _identifier_is_asserted(sentence: str, field: str, identifier: str,
                            *, group_qualified: bool = False) -> bool:
    identifier_pattern = re.compile(
        rf"(?:(?<![A-Za-z0-9._:/-])|(?<=synthetic-only:))"
        rf"{re.escape(identifier)}(?![A-Za-z0-9_:/-])(?!\.[A-Za-z0-9])")
    identifier_match = identifier_pattern.search(sentence)
    if identifier_match is None:
        return False

    role_patterns = {
        "commit": r"(?<![A-Za-z0-9_-])(?:commit(?:\s+id)?|커밋)(?![A-Za-z0-9_-])",
        "deployment": r"(?<![A-Za-z0-9_-])(?:deployment|deploy|배포)(?![A-Za-z0-9_-])",
        "serving": r"(?<![A-Za-z0-9_-])(?:serving|served|서빙)(?![A-Za-z0-9_-])",
    }
    marker_pattern = re.compile(
        r"(?<![A-Za-z0-9_-])(?:synthetic|fixture|합성)(?![A-Za-z0-9_-])",
        flags=re.IGNORECASE)
    relation_pattern = re.compile(
        r":|=|\b(?:value|id|uri|is|was)\b|값(?:은|이|을|:|=)?",
        flags=re.IGNORECASE)
    for role_match in re.finditer(role_patterns[field], sentence, flags=re.IGNORECASE):
        if role_match.end() > identifier_match.start():
            continue
        relation = sentence[role_match.end():identifier_match.start()]
        if len(relation) > 72 or relation_pattern.search(relation) is None:
            continue
        local_qualifier = any(
            marker.end() <= identifier_match.start()
            and role_match.start() - 24 <= marker.start() <= role_match.start() + 24
            for marker in marker_pattern.finditer(sentence))
        if not local_qualifier and not group_qualified:
            continue

        before_role = sentence[max(0, role_match.start() - 24):role_match.start()]
        after_identifier = sentence[identifier_match.end():identifier_match.end() + 24]
        if _has_any(before_role, (r"\b(?:no|not|never|without)\s+(?:a\s+|the\s+)?$",
                                  r"(?:아닌|아니라|없는)\s*$")):
            continue
        if _has_any(relation, (r"\b(?:not|never|no|without)\b", r"(?:아닌|아니라|없는|않)")
                    ) or _has_any(after_identifier, (r"^\s*(?:is|was)\s+(?:not|never)\b",
                                                      r"^\s*(?:이|가)?\s*(?:아니|아닌|아니라)",
                                                      r"^\s*않")):
            continue
        return True
    return False


def _group_synthetic_qualifier(text: str) -> bool:
    return _has_any(text, (
        r"\ball\s+(?:of\s+)?(?:the\s+)?(?:displayed|listed|shown)\s+values?\b"
        r".{0,48}\b(?:synthetic|fixture)\b",
        r"\b(?:all|these)\s+values?\b.{0,48}\b(?:synthetic|fixture)\b",
        r"모든\s*(?:표시된\s*)?값.{0,32}(?:합성|fixture)",
    ))


def _file_state(section: str, state: str) -> bool:
    text = section.casefold()
    target = r"(?:follow[- ]?through(?:\.json)?|file|파일|후속)"
    negated_absence = _has_any(text, (
        r"\bnot\s+(?:absent|missing|nonexistent|non-existent)\b",
        r"\b(?:isn't|wasn't|aren't|weren't)\s+(?:absent|missing)\b",
        r"없지\s*않|없지는\s*않|존재하지\s*않는\s*것은\s*아니|부재하지\s*않|누락되지\s*않",
    ))
    negated_presence = _has_any(text, (
        r"\b(?:not|never)\s+(?:present|created|available|there)\b",
        r"\b(?:did|does|do)\s+not\s+exist\b|\b(?:didn't|doesn't|don't)\s+exist\b",
        r"\b(?:wasn't|isn't|aren't|weren't)\s+(?:present|created|available|there)\b",
        r"생성되지|생성하지\s*않|있지\s*않|존재하지",
    ))
    absent = _has_any(text, (
        r"\babsent\b|\bmissing\b",
        r"\bno\s+(?:follow[- ]?through\s+)?file\b",
        r"\b(?:did|does|do)\s+not\s+exist\b|\b(?:didn't|doesn't|don't)\s+exist\b",
        r"\b(?:was|is|are|were)\s+not\s+(?:present|created|available|there)\b",
        r"\b(?:wasn't|isn't|aren't|weren't)\s+(?:present|created|available|there)\b",
        rf"{target}.{{0,24}}(?:없|존재하지|생성되지|만들어지지|있지\s*않)",
        r"(?:없|존재하지|생성되지|만들어지지|있지\s*않).{0,24}" + target,
    ))
    present = _has_any(text, (
        rf"{target}.{{0,32}}\b(?:present|exists|available|created|there)\b",
        rf"\b(?:present|exists|available|created|there)\b.{{0,32}}{target}",
        rf"{target}.{{0,24}}(?:생성|있|존재)",
        rf"(?:생성|있|존재).{{0,24}}{target}",
    ))
    if state == "absent":
        if negated_absence or (present and not negated_presence):
            return False
        return absent
    if state == "present":
        if negated_presence or absent:
            return False
        return present
    raise ValueError(f"unsupported file state: {state}")


def _completion_is_negated(text: str) -> bool:
    return _has_any(text, (
        r"\b(?:did|does|do|was|were|is|are|has|have|had)?\s*(?:not|never|didn't|doesn't|"
        r"don't|wasn't|weren't|isn't|aren't)\s+(?:actually\s+)?"
        r"(?:complete|completed|finish|finished|fulfill|fulfilled|perform|performed|"
        r"execute|executed|carry\s+out)\b.{0,80}\b(?:follow[- ]?through|follow[- ]?up|"
        r"continuation|(?:approved|requested)\s+(?:(?:synthetic|fixture)\s+)?task|"
        r"(?:approved|requested)\s+replay)\b",
        r"\b(?:follow[- ]?through|follow[- ]?up|continuation|"
        r"(?:approved|requested)\s+(?:(?:synthetic|fixture)\s+)?task|"
        r"(?:approved|requested)\s+replay)\b"
        r".{0,64}\b(?:(?:was|were|is|are|has\s+been|have\s+been)\s+)?"
        r"(?:not|never)\s+(?:completed|finished|fulfilled|performed|executed|carried\s+out)\b",
        r"(?:후속|승인된\s*작업|요청한\s*작업).{0,48}(?:완료|마치|처리|수행)(?:하지|되지)\s*(?:않|못)",
        r"(?:완료|마치|처리|수행)(?:하지|되지)\s*(?:않|못).{0,48}(?:후속|승인된\s*작업|요청한\s*작업)",
    ))


def _operating_limit_claims(prose: str) -> tuple[bool, bool]:
    negative_patterns = (
        r"\bno\b.{0,180}\b(?:production|operating|operational)\b.{0,120}"
        r"\b(?:changed|measured|probed|altered|modified|deployed|served)\b",
        r"\b(?:production|operating|operational)\b.{0,120}\b"
        r"(?:was|were|is|are|has\s+been|have\s+been|did|does|do)?\s*"
        r"(?:not|never|wasn't|weren't|isn't|aren't|didn't|doesn't|don't)\s+"
        r"(?:change|changed|measure|measured|probe|probed|alter|altered|modify|modified)\b",
        r"\b(?:production|operating|operational)\b.{0,80}\b(?:unchanged|unmodified|unmeasured)\b",
        r"운영.{0,120}(?:변경|측정|조사|확인|배포|서빙).{0,24}(?:않|못|없)",
        r"운영.{0,100}(?:변하지|측정되지|변경되지|조사되지|확인되지)\s*않",
    )
    activity = re.compile(
        r"\b(?:change|changed|measure|measured|probe|probed|alter|altered|modify|modified|"
        r"deploy|deployed|serve|served)\b|변경|측정|조사|확인", re.IGNORECASE)
    subjects = re.compile(r"\b(?:production|operating|operational)\b|운영", re.IGNORECASE)
    clauses = []
    for sentence in _prose_sentences(prose):
        clauses.extend(re.split(
            r"\s+(?:but|however|although|whereas|yet|하지만|그러나|반면|다만)\s+|;\s*",
            sentence, flags=re.IGNORECASE))

    has_negative = False
    has_positive = False
    for clause in clauses:
        if not _has_any(clause, (r"\b(?:production|operating|operational)\b|운영",)):
            continue
        if _has_any(clause, negative_patterns):
            has_negative = True
        for subject in subjects.finditer(clause):
            for verb in activity.finditer(clause, subject.end(), min(len(clause), subject.end() + 180)):
                between = clause[subject.end():verb.start()]
                after = clause[verb.end():min(len(clause), verb.end() + 28)]
                before = clause[max(0, subject.start() - 120):subject.start()]
                negated = (_has_any(between, (r"\b(?:not|never|no|without)\b", r"않|못"))
                           or _has_any(after, (r"\b(?:not|never)\b", r"않|못"))
                           or _has_any(before, (r"\bno\b",)))
                if not negated:
                    has_positive = True
    return has_negative, has_positive


def _count_value(section: str, count_terms: str, value: int) -> bool:
    words = {0: ("zero", "no", "영"), 1: ("one", "a single", "once", "한 번")}[value]
    number = r"(?<!\d)" + str(value) + r"(?!\d)"
    forms = "|".join([number, *(rf"\b{re.escape(word)}\b" if word.isascii()
                          else re.escape(word) for word in words)])
    return bool(re.search(rf"(?:{count_terms}).{{0,24}}(?:{forms})|(?:{forms}).{{0,24}}(?:{count_terms})",
                          section, flags=re.IGNORECASE))


def _report_content_checks(prose: str, expected: dict) -> tuple[dict, dict]:
    windows = _prose_windows(prose)
    reason_parts = (
        (r"approved|authorized|agreed|승인",),
        (r"follow[- ]?through|follow[- ]?up|continuation|후속|이어",),
        (r"complete|completed|finish|finished|fulfill|carry out|마치|완료|처리|수행",),
        (r"replay|requested task|parent|task|재현|요청한 작업|작업|부모",),
        (r"\bto\b|\bso\b|\bfor\b|\bafter\b|위해|목적|때문",),
    )
    explains_reason = any(
        all(_has_any(window, patterns) for patterns in reason_parts)
        and not _completion_is_negated(window)
        for window in windows)

    count_terms = (r"action count|execution count|run count|follow[- ]?through count|"
                   r"실행 횟수|작업 횟수|행동 수|실행 수")
    before_and_after = (
        any(_file_state(section, "absent") and _count_value(section, count_terms, 0)
            for section in _label_sections(prose, "before"))
        and any(_file_state(section, "present") and _count_value(section, count_terms, 1)
                for section in _label_sections(prose, "after")))

    verification = expected["verification"]
    number = r"(?<!\d)" + str(verification["checks_passed"]) + r"(?!\d)"
    six_words = r"\bsix\b|\b6\b|여섯|육"
    passed = r"\bpass(?:ed|ing)?\b|\bsucceed(?:ed|s)?\b|\bsuccessful\b|통과|성공"
    pass_negation = (r"\b(?:not|never|did\s+not|didn't|failed\s+to)\s+"
                     r"(?:all\s+)?(?:the\s+)?(?:checks?\s+)?pass(?:ed)?\b|"
                     r"\b(?:all\s+)?(?:six|6)\s+checks?\s+(?:did\s+not\s+pass|failed)\b|"
                     r"통과하지\s*않|성공하지\s*않|모든\s*검사가\s*통과하지\s*않")
    failure_claim = (r"\b[1-9]\d*\s+(?:checks?\s+)?(?:failed|failures?)\b|"
                     r"\b(?:failed|failures?)\s*(?:=|:)?\s*[1-9]\d*\b|"
                     r"\bchecks?\s+failed\b|\b(?:some|several|many)\s+checks?\s+failed\b|"
                     r"[1-9]\d*건\s*실패|실패(?:했|하였|하였다|함)")
    reports_verification = any(
        _has_any(window, (r"verification|verified|verifier|checks?|검증",))
        and _has_any(window, (number, six_words))
        and _has_any(window, (passed,))
        and not _has_any(window, (pass_negation,))
        and not _has_any(window, (failure_claim,))
        for window in windows)

    value_fields = {
        "commit": expected["synthetic_values"]["commit"],
        "deployment": expected["synthetic_values"]["deployment"],
        "serving": expected["synthetic_values"]["serving"],
    }
    identifiers = {key: _expected_identifier(key, value)
                   for key, value in value_fields.items()}
    value_sentences = []
    for paragraph in re.split(r"\n\s*\n", prose):
        sentences = _raw_prose_sentences(paragraph)
        qualified_indices = [index for index, sentence in enumerate(sentences)
                             if _group_synthetic_qualifier(sentence)]
        for index, sentence in enumerate(sentences):
            group_qualified = any(abs(index - qualifier_index) <= 4
                                  for qualifier_index in qualified_indices)
            value_sentences.append((sentence, group_qualified))
    values_in_prose = {
        key: bool(identifier) and any(
            _identifier_is_asserted(sentence, key, identifier,
                                    group_qualified=group_qualified)
            for sentence, group_qualified in value_sentences)
        for key, identifier in identifiers.items()
    }

    skipped = any(
        _has_any(window, (r"skipped|omitted|not run|생략|실행하지",))
        and _has_any(window, (r"production|operational|운영",))
        and _has_any(window, (r"deployment|deploy|배포|checks?|검사|확인",))
        and _has_any(window, (r"serving|서빙",))
        for window in windows)
    negative_operating_claim, positive_operating_change = _operating_limit_claims(prose)
    no_operating_change = negative_operating_claim and not positive_operating_change
    replay_scope_limit = any(
        _has_any(window, (r"isolated|격리",))
        and _has_any(window, (r"replay|재현",))
        for window in windows)
    replay_scope_limit = replay_scope_limit and no_operating_change
    content_checks = {
        "explains_fixture_reason": explains_reason,
        "describes_before_state_and_zero_actions": before_and_after,
        "reports_six_check_verification_result": reports_verification,
        "names_actual_synthetic_values": all(values_in_prose.values()),
        "states_fixture_skip_and_operating_limit": skipped and no_operating_change,
        "states_isolated_replay_scope_limit": replay_scope_limit,
    }
    return content_checks, values_in_prose


def _first_report_checks(text: str, tools: list[dict], fixture: dict) -> dict:
    expected = fixture["facts"]["report"]
    report = _factual_report_object(text)
    artifact_read_evidence = {}
    for path in [fixture["facts_path"], *fixture["artifact_paths"]]:
        artifact_read_evidence[str(path)] = [evidence for item in tools
                                             if (evidence := _read_evidence(item, path)) is not None]
    artifact_reads = {path: bool(evidence) for path, evidence in artifact_read_evidence.items()}
    verification = expected["verification"]
    verifier_marker = (f"FIXTURE_VERIFICATION={verification['result']} "
                      f"checks={verification['checks_passed']} failed={verification['checks_failed']}")
    verifier_invocations = _script_invocations(tools, fixture["verifier"], verifier_marker)
    action_invocations = _script_invocations(tools, fixture["action"])
    verifier_hash_matches = _file_hash_matches(fixture["verifier"], fixture["verifier_sha256"])
    action_hash_matches = _file_hash_matches(fixture["action"], fixture["action_sha256"])
    followthrough = None
    try:
        followthrough = json.loads(fixture["followthrough"].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    prose = text.split("FACTUAL_REPORT_JSON:", 1)[0].strip()
    prose_content_checks, values_in_prose = _report_content_checks(prose, expected)
    return {
        "report_matches_fixture_facts": report == expected,
        "report_has_substantive_closing_prose": bool(prose) and all(
            prose_content_checks.values()),
        "prose_content_checks": prose_content_checks,
        "synthetic_value_checks": values_in_prose,
        "every_exact_artifact_was_read": bool(artifact_reads) and all(artifact_reads.values()),
        "artifact_reads": artifact_reads,
        "artifact_read_evidence": artifact_read_evidence,
        "fixture_verifier_passed_in_native_tool_trace": (
            verifier_hash_matches and any(
                invocation["execution_result_proven"]
                and invocation["tool_succeeded"]
                and invocation["output_marker_observed"]
                for invocation in verifier_invocations)),
        "verifier_script_unchanged": verifier_hash_matches,
        "verifier_invocations": verifier_invocations,
        "one_approved_followthrough_tool_invocation": (
            len(action_invocations) == 1 and action_invocations[0]["tool_succeeded"]
            and action_hash_matches),
        "followthrough_script_unchanged": action_hash_matches,
        "followthrough_invocations": action_invocations,
        "followthrough_state_matches_one_time_fixture_action": followthrough == {
            "fixture_id": fixture["facts"]["fixture_id"],
            "sentinel": fixture["facts"]["sentinel"],
            "approved_action_count": expected["after"]["approved_action_count"], "synthetic": True},
        "report_object": report,
    }


def _fixture_registry(jobs: Path, thread_id: str, workspace: Path, sentinel: str):
    attempt = "att-parent-replay-" + uuid.uuid4().hex[:12]
    route_id = "rt-parent-replay-" + uuid.uuid4().hex[:8]
    route_hash = "sha256:" + hashlib.sha256(route_id.encode()).hexdigest()
    revision = "revision-" + hashlib.sha256((attempt + route_id).encode()).hexdigest()[:16]
    delivery_id = "delivery-" + hashlib.sha256((thread_id + attempt).encode()).hexdigest()[:32]
    delivery_parent = "parent-session-" + hashlib.sha256(thread_id.encode()).hexdigest()
    receipt = {"schema_version": 2, "state": "ready", "parent_attempt_id": delivery_parent,
        "job_registry": str(jobs), "delivery_classification": "success",
        "children": [{"attempt_id": attempt, "status": "done", "readiness": "ready",
            "reason": "registry-closed", "required_action": "advance-completed",
            "harness": "codex", "delivery_classification": "success"}]}
    digest = pending_delivery._canonical_receipt_digest(receipt)
    sealed_batch_id = "batch-parent-replay-" + hashlib.sha256(attempt.encode()).hexdigest()[:12]
    metadata = {
        "attempt_schema_version": "2", "dispatch_depth": "1", "transport": "headless",
        "execution_surface": "registered-headless", "registered_worker": "1",
        "launch_claimed": "1", "launch_started": "1", "worker_type": "owner",
        "attempt_id": attempt, "parent_sid": thread_id, "parent_completion_delivery": "codex-native-queue",
        "delivery_intent": "1", "delivery_id": delivery_id,
        "delivery_receipt_digest": digest, "delivery_row_revision": revision,
        "delivery_recipient_kind": "codex-native-queue", "route_id": route_id,
        "owner_route_id": route_id, "owner_route_hash": route_hash,
        "route_hash": route_hash, "route_node": "_owner", "harness": "codex",
        "workflow_completion": "runtime-v1", "managed_sealed_batch_id": sealed_batch_id,
        "note": "completed-marker", "failure_class": "pass", "cleanup_state": "unknown",
        "launch_outcome": "completed", "terminal_event": "worker-result",
    }
    jobs.write_text("2026-10-19T00:00:00Z\tdone\t" + str(workspace) + "\t" + str(workspace)
        + "\tisolated-parent-replay\t" + ",".join(f"{key}={value}" for key, value in metadata.items()) + "\n",
        encoding="utf-8")
    record = pending_delivery.create(jobs.resolve(strict=True).parent,
        recipient_kind="codex-native-queue", recipient_key=thread_id, delivery_id=delivery_id,
        session_generation="", session_generation_supported="0", attempt_ids=[attempt],
        parent_attempt_id="-", route_id=route_id, route_node="_owner", receipt=receipt,
        receipt_digest=digest, row_revisions={attempt: revision})
    return attempt, route_id, receipt, record, metadata


def _record_case(report: dict, output_dir: Path, case_id: str, checks: dict[str, bool],
                 *, evidence: list[str] | None = None, status: str | None = None,
                 reason: str | None = None) -> dict:
    if status is None:
        status = "PASS" if all(checks.values()) else "FAIL"
    if reason is None:
        failed = [name for name, passed in checks.items() if not passed]
        reason = "all checks passed" if not failed else "failed checks: " + ", ".join(failed)
    result = {"status": status, "reason": reason, "checks": checks,
              "evidence": evidence or []}
    report.setdefault("replay_cases", {})[case_id] = result
    report["case_namespace"] = (
        "replay_cases d1-d3 are native replay checks; route completion ids d1-d5 are separate")
    if case_id.startswith("d1_"):
        report["d1"] = status
    elif case_id.startswith("d2_"):
        report["d2"] = status
    elif case_id.startswith("d3_"):
        report["d3"] = status
    _write_json(output_dir / "replay.json", report)
    return result


def _evaluate_replay_case(case_id: str, observation: dict) -> dict[str, bool]:
    """Turn native transport observations into the production case decisions."""
    if case_id == "d1_first_report":
        return {key: value for key, value in observation.items() if isinstance(value, bool)}
    if case_id == "d2_duplicate_cleanup":
        return {
            "late_deadline_classified_as_cleanup_only": (
                not observation["cleanup_is_current"]
                and "retains cleanup observation only" in observation["cleanup_text"]),
            "same_result_redelivery_preserved_exact_ack": (
                observation["duplicate_state"] == "acked"
                and observation["duplicate_kept_exact_ack"]),
            "native_courier_rejected_resolved_cleanup": (
                observation["cleanup_courier_state"] == "rejected"
                and observation["cleanup_courier_reason"] == "supervision-resolved"),
            "old_ack_preserved_after_cleanup": observation["old_ack_preserved"],
            "cleanup_created_no_extra_turn": observation["no_extra_turn"],
            "cleanup_queue_item_suppressed": observation["no_cleanup_queue_item"],
            "completion_duplicate_queue_item_suppressed": observation["no_queued_duplicate"],
        }
    if case_id == "d3_new_conflict":
        checks = {
            "conflict_fixture_preserved_done_row_and_new_failure": observation["conflict_fixture_valid"],
            "new_notice_is_distinct_and_current": observation["notice_is_distinct_current"],
            "new_notice_received_exact_ack": observation["notice_ack_observed"],
            "new_notice_turn_completed": observation["notice_turn_status"] == "completed",
            "new_notice_has_response": observation["notice_has_response"],
            "notice_turn_occurs_once": observation["notice_occurrences"] == 1,
            "old_ack_identity_preserved": observation["old_ack_preserved"],
            "no_duplicate_action_or_response": observation["no_duplicate_action_or_response"],
        }
        for name in ("new_failure_conflict_remains_visible",
                     "original_ack_identity_still_preserved"):
            if name in observation:
                checks[name] = observation[name]
        return checks
    if case_id == "direct_question":
        answer = observation["answer"].casefold()
        return {
            "answers_from_exact_production_status": (
                "production" in answer
                and any(token in answer for token in ("no", "not", "fixture", "unmeasured"))),
            "does_not_repeat_followthrough": observation["followthrough_unchanged"],
            "no_followthrough_action_invocation": observation["action_invocations"] == 0,
        }
    raise ValueError("unknown replay case: " + case_id)


def _run_replay_case_sequence(report: dict, output_dir: Path, steps: list[dict]) -> dict:
    """Evaluate and persist ordered cases; failures do not gate independent steps."""
    for index, step in enumerate(steps):
        case_id = step["case_id"]
        missing = [name for name, available in step.get("prerequisites", []) if not available()]
        if missing:
            _record_case(report, output_dir, case_id, {"prerequisites_available": False},
                         status="BLOCKED", reason="missing prerequisite state: " + ", ".join(missing),
                         evidence=step.get("evidence"))
            continue
        if step.get("skip_evaluation"):
            _record_case(report, output_dir, case_id, step.get("checks", {}),
                         status=step.get("status", "BLOCKED"), reason=step.get("reason"),
                         evidence=step.get("evidence"))
            continue
        try:
            observation = step["observe"]()
            checks = _evaluate_replay_case(case_id, observation)
        except ReplayFailed as exc:
            _record_case(report, output_dir, case_id, {"case_evaluation_completed": False},
                         status="FAIL", reason=str(exc), evidence=step.get("evidence"))
            continue
        except Exception as exc:  # preserve prior cases and stop at lost transport
            _record_case(report, output_dir, case_id, {"transport_observation_available": False},
                         status="BLOCKED", reason=str(exc) or type(exc).__name__,
                         evidence=step.get("evidence"))
            for pending in steps[index + 1:]:
                _record_case(report, output_dir, pending["case_id"], {}, status="not-run",
                             reason="not evaluated after transport stopped at " + case_id,
                             evidence=pending.get("evidence"))
            break
        _record_case(report, output_dir, case_id, checks, evidence=step.get("evidence"),
                     status=step.get("status"), reason=step.get("reason"))
    return report


def _aggregate_replay_status(report: dict, acceptance_checks: dict[str, bool]) -> str:
    statuses = [result.get("status") for result in report.get("replay_cases", {}).values()
                if isinstance(result, dict)]
    if "FAIL" in statuses:
        return "FAIL"
    if any(status in {"BLOCKED", "not-run"} for status in statuses):
        return "BLOCKED"
    if not all(acceptance_checks.values()):
        return "BLOCKED"
    return "PASS"


def _case_can_continue(result: dict) -> bool:
    """A measured PASS or FAIL does not erase independent later cases."""
    return result.get("status") in {"PASS", "FAIL"}


def _record_unfinished_cases(report: dict, output_dir: Path, reason: str) -> None:
    for case_id in ("d1_first_report", "d2_duplicate_cleanup", "d3_new_conflict",
                    "direct_question", "overall_acceptance"):
        if case_id not in report.get("replay_cases", {}):
            _record_case(report, output_dir, case_id, {}, status="not-run", reason=reason)


def _has_failed_case(report: dict) -> dict | None:
    for case_id, result in report.get("replay_cases", {}).items():
        if isinstance(result, dict) and result.get("status") == "FAIL":
            return {"case_id": case_id, **result}
    return None


def _finish_after_runtime_error(report: dict, output_dir: Path, kind: str,
                                reason: str, detail: str | None = None) -> dict:
    _record_unfinished_cases(report, output_dir,
                             f"not evaluated after {kind.lower()} runtime stop: {reason}")
    observed_failure = _has_failed_case(report)
    report["operating_records_changed"] = False
    if observed_failure:
        report.update(status="failed", verdict="FAIL",
                      failure_reason=(f"observed {observed_failure['case_id']} failure: "
                                      f"{observed_failure['reason']}"),
                      later_runtime_state=f"{kind}: {reason}")
    elif kind == "FAIL":
        report.update(status="failed", verdict="FAIL", failure_reason=reason)
    else:
        report.update(status="blocked", verdict="BLOCKED", blocker=reason)
    if detail:
        report["detail"] = detail[:240]
    return report


def _run_replay_impl(output_dir: Path) -> dict:
    report = {"schema_version": 1, "source_sha": "unknown", "status": "running",
              "verdict": "BLOCKED", "d1": "unfinished", "d2": "unfinished",
              "d3": "not-assessed", "replay_cases": {},
              "case_namespace": "replay_cases d1-d3 are native replay checks; route completion ids d1-d5 are separate",
              "operating_records_changed": False,
              "model": "default (not independently identified)", "limitations": []}
    _write_json(output_dir / "replay.json", report)
    try:
        report["source_sha"] = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
            capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        diff = subprocess.run(["git", "diff", "--binary", "HEAD"], cwd=ROOT,
                              capture_output=True, timeout=10, check=True).stdout
        untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=ROOT,
                                   capture_output=True, text=True, timeout=10, check=True).stdout.splitlines()
        digest = hashlib.sha256(diff)
        for relative in sorted(untracked):
            path = ROOT / relative
            if path.is_file() and not path.is_symlink():
                digest.update(relative.encode("utf-8") + b"\0" + path.read_bytes())
        report["source_tree_sha256"] = digest.hexdigest()
    except (OSError, subprocess.SubprocessError):
        report["source_tree_sha256"] = "unavailable"
    codex = shutil.which("codex")
    if not codex:
        raise ReplayBlocked("codex-executable-unavailable")
    version = subprocess.run([codex, "--version"], capture_output=True, text=True,
                             timeout=10, check=False)
    report["codex_version"] = (version.stdout or version.stderr).strip()[:200]
    if version.returncode != 0:
        raise ReplayBlocked("codex-version-probe-failed")
    _write_json(output_dir / "replay.json", report)

    shared_directory = Path("/tmp").resolve() / f"codex-daemon-{os.geteuid()}"
    if shared_directory.exists() and stat.S_IMODE(shared_directory.stat().st_mode) == 0:
        # The outer native sandbox deliberately masks privileged daemon IPC.
        # Private CODEX_HOME modes cannot repair it; never unmask that boundary.
        raise ReplayBlocked("nested-codex-shared-daemon-directory-masked")

    source_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()
    # The private runtime fixture is removed after the replay; only evidence
    # remains under output_dir. /tmp is the writable temp root in this runner.
    with tempfile.TemporaryDirectory(prefix="codex-parent-replay-", dir="/tmp") as temporary:
        temp_root = Path(temporary)
        home = temp_root / "home"
        codex_home = home / ".codex"
        worktree = temp_root / "workspace"
        runtime = temp_root / "runtime"
        for path in (home, codex_home, worktree, runtime, home / ".config", home / ".state"):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(worktree)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10)
        copied, auth_source = _copy_runtime_config(source_home, codex_home, dict(os.environ))
        if not copied.get("auth.json"):
            raise ReplayBlocked("isolated-codex-auth-unavailable")
        trusted_project = str(worktree).replace('\\', '\\\\').replace('"', '\\"')
        (codex_home / "config.toml").write_text(
            f'[projects."{trusted_project}"]\ntrust_level = "trusted"\n\n'
            "[analytics]\nenabled = false\n\n[features]\napps = false\nmulti_agent = false\n",
            encoding="utf-8")
        agent_home = _fake_agent_home(temp_root)
        workflow = (ROOT / "core/WORKFLOW.md").read_text(encoding="utf-8")
        report_policy = workflow.split("### 0.5. Post-Execution Completion Report", 1)[1].split(
            "### 0.6.", 1)[0]
        response_policy = (ROOT / "roles/response-policy.md").read_text(encoding="utf-8")
        # Exercise candidate policy in native project context, not an ideal
        # reporting rubric supplied in the user's task message.
        policy = "# Candidate completion policy\n" + report_policy + "\n" + response_policy
        (worktree / "AGENTS.md").write_text(policy, encoding="utf-8")
        report["candidate_policy_sha256"] = hashlib.sha256(policy.encode()).hexdigest()
        _write_json(output_dir / "replay.json", report)
        jobs = runtime / "jobs.log"
        jobs.touch(mode=0o600)
        artifact_root = worktree / ".agent_reports"
        artifact_root.mkdir(mode=0o700)
        sentinel = "HEARTING_REPLAY_" + uuid.uuid4().hex.upper()
        fixture = _write_native_fixture(worktree, sentinel)
        artifact_paths = [fixture["facts_path"], *fixture["artifact_paths"]]
        for event in fixture["facts"]["events"]:
            _append_event(output_dir / "native-events.jsonl", {
                "event": "synthetic-fixture-stage", "sequence": event["sequence"],
                "stage": event["event"], "result": event["result"],
                "evidence": event["evidence"], "synthetic": True,
            })
        followthrough = fixture["followthrough"]
        endpoint = codex_home / "app-server-control" / "app-server-control.sock"
        # Do not inherit the supervised worker's CODEX_HOME, artifact output,
        # memory, Herd, or provider-key bindings into the isolated app server.
        env = {key: os.environ[key] for key in
               ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "USER", "LOGNAME", "SHELL")
               if key in os.environ}
        env.update({
            "HOME": str(home), "CODEX_HOME": str(codex_home), "AGENT_HOME": str(agent_home),
            "AGENT_DISPATCH_JOBS": str(jobs), "AGENT_ARTIFACT_ROOT": str(artifact_root),
            "AGENT_ARTIFACT_CHECKPOINT": "off", "AGENT_DISPATCH_CALLER_HARNESS": "codex",
            "XDG_CONFIG_HOME": str(home / ".config"), "XDG_STATE_HOME": str(home / ".state"),
            "XDG_CACHE_HOME": str(home / ".cache"), "XDG_DATA_HOME": str(home / ".local/share"),
            "TMPDIR": str(temp_root / "tmp"),
        })
        Path(env["TMPDIR"]).mkdir(mode=0o700, parents=True, exist_ok=True)
        hooks_path = codex_home / "hooks.json"
        if not hooks_path.is_file():
            raise ReplayBlocked("isolated-codex-hooks-config-unavailable")
        env["HEARTING_CODEX_HOOK_SOURCES"] = json.dumps([str(codex_home / "config.toml"), str(hooks_path)])
        # The bare UDS transport uses CODEX_HOME's private app-server-control
        # directory, matching codex_queue_dispatch.resolve_queue_socket.
        command = [codex, "app-server", "--listen", "unix://",
                   "--disable", "apps", "--disable", "multi_agent"]
        server = _start_server(command, endpoint, worktree, env,
                               temp_root / "app-server-initial.stderr")
        try:
            client = _rpc_session(endpoint)
            try:
                # Initialization has loaded the auth record into the isolated
                # server. Remove the private copy before model tools can run.
                (codex_home / "auth.json").unlink(missing_ok=True)
                # Codex exposes project hook hashes only after project trust is
                # established by a thread. Probe without a model turn, persist
                # exact current hashes through hooks.state, then start the
                # measured thread with native hook trust already in place.
                thread_id, probe_thread_id, hook_trust = _start_trusted_replay_thread(
                    client, codex_home, worktree, hooks_path)
                _append_event(output_dir / "native-events.jsonl", {
                    "event": "isolated-hooks-trusted", "probe_thread_id": probe_thread_id,
                    **hook_trust,
                })
                attempt, route_id, receipt, completion_record, metadata = _fixture_registry(
                    jobs, thread_id, worktree, sentinel)
                prompt = (
                    "Continue the already approved isolated fixture task. Read every exact path below, "
                    "including fixture-facts.json, in the listed order. Run the exact verify_fixture.py "
                    "with Python 3. Before any action, check whether follow-through.json already exists. "
                    "If absent, run apply_fixture_followthrough.py with the exact sentinel from the facts "
                    "once; if present and exact, do not run it again. Then give the first substantive "
                    "completion report, including why, before/after, verification, skips, synthetic "
                    "commit/deployment/serving values, and limits. After the prose, include a JSON object "
                    "under the exact marker FACTUAL_REPORT_JSON: that matches the report object in "
                    "fixture-facts.json. Label every fixture value synthetic and do not imply any real "
                    "commit, deployment, independent review, production serving, CI, or operating change.\n"
                    "Exact artifact paths (read all):\n- "
                    + "\n- ".join(str(path) for path in artifact_paths)
                    + f"\nVerifier: {fixture['verifier']}\nOne-time approved action: {fixture['action']} {sentinel}\n"
                    + f"Expected action file: {followthrough}\n"
                    "The earlier chronology is isolated fixture history; do not invent external results."
                )
                (output_dir / "input.txt").write_text(prompt + "\n", encoding="utf-8")
                started = client.request("turn/start", {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": prompt, "text_elements": []}],
                    "cwd": str(worktree), "approvalPolicy": "never",
                    "sandboxPolicy": {"type": "workspaceWrite", "writableRoots": [str(worktree)],
                                      "networkAccess": False, "excludeTmpdirEnvVar": False,
                                      "excludeSlashTmp": False},
                })
                turn = started.get("turn")
                if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
                    raise ReplayBlocked("isolated-turn-start-unconfirmed")
                turn_id = turn["id"]
                _append_event(output_dir / "native-events.jsonl", {
                    "event": "turn-started", "thread_id": thread_id, "turn_id": turn_id,
                    "attempt_id": attempt, "delivery_id": completion_record["delivery_id"],
                    "receipt_digest": completion_record["receipt_digest"],
                })
            finally:
                client.close()

            from peer_obligations import ObligationStore, stable_duty_id
            duty_identity = {"jobs": str(jobs.resolve()), "attempt_id": attempt,
                             "session_id": thread_id, "harness": "codex", "server": "default"}
            obligations = ObligationStore(runtime)
            duty_id = stable_duty_id("registered-batch", duty_identity)
            ack_deadline = time.monotonic() + 60
            first_record = None
            first_turn_before_crash = None
            duty = None
            while time.monotonic() < ack_deadline:
                first_record = pending_delivery.read(runtime, thread_id, completion_record["delivery_id"])
                duty = obligations.get(duty_id)
                binding = ((duty or {}).get("observation") or {}).get("native_receiving_turn") or {}
                if (first_record and first_record.get("state") == "acked"
                        and binding.get("thread_id") == thread_id
                        and binding.get("turn_id") == turn_id
                        and binding.get("delivery_id") == completion_record["delivery_id"]):
                    first_turn_before_crash = _turn_record(endpoint, thread_id, turn_id)
                    break
                time.sleep(0.005)
            if first_record is None or first_record.get("state") != "acked":
                raise ReplayBlocked("native-hook-exact-ack-not-observed-before-turn-end")
            if not isinstance(duty, dict) or not ((duty.get("observation") or {}).get("native_receiving_turn")):
                raise ReplayBlocked("native-receiving-turn-not-retained-before-ack")
            if not first_turn_before_crash or first_turn_before_crash.get("status") == "completed":
                raise ReplayBlocked("native-app-server-closed-crash-window-before-isolated-kill")
            initial_messages, initial_tools = _turn_items(first_turn_before_crash)
            _write_json(output_dir / "first-turn-before-crash.json", first_turn_before_crash)
            _append_event(output_dir / "native-events.jsonl", {
                "event": "ack-before-parent-crash", "turn_id": turn_id,
                "status": first_turn_before_crash.get("status"),
                "precrash_agent_messages": len(initial_messages),
                "precrash_tool_items": len(initial_tools),
                "acked_by": first_record.get("acked_by"),
                "receiving_turn": (duty.get("observation") or {}).get("native_receiving_turn"),
            })
            _stop_server(server)
            server = None
            _append_event(output_dir / "native-events.jsonl", {
                "event": "isolated-app-server-killed", "signal": "SIGKILL", "thread_id": thread_id})

            if auth_source is None or not _copy_regular(auth_source, codex_home / "auth.json", private=True):
                raise ReplayBlocked("isolated-codex-auth-unavailable-after-restart")
            server = _start_server(command, endpoint, worktree, env,
                                   temp_root / "app-server-reconnect.stderr")
            interrupted_turn = _turn_record(endpoint, thread_id, turn_id)
            if not interrupted_turn or interrupted_turn.get("status") != "interrupted":
                raise ReplayBlocked("native-turn-history-does-not-prove-interrupted-crash")
            client = _rpc_session(endpoint)
            try:
                (codex_home / "auth.json").unlink(missing_ok=True)
                for reconnect_index in (1, 2):
                    resumed = client.request("thread/resume", {
                        "threadId": thread_id, "cwd": str(worktree),
                        "approvalPolicy": "never", "sandbox": "workspace-write",
                    })
                    resumed_thread = resumed.get("thread")
                    if not isinstance(resumed_thread, dict) or resumed_thread.get("id") != thread_id:
                        raise ReplayBlocked("native-sessionstart-resume-binding-mismatch")
                    _append_event(output_dir / "native-events.jsonl", {
                        "event": "native-thread-resumed", "thread_id": thread_id,
                        "reconnect_index": reconnect_index})
                    time.sleep(0.1)
            finally:
                client.close()

            continuation_deadline = time.monotonic() + 60
            continuation_id = ""
            duty = None
            while time.monotonic() < continuation_deadline:
                duty = obligations.get(duty_id)
                observation = (duty or {}).get("observation") or {}
                candidate = observation.get("continuation_id")
                if isinstance(candidate, str) and candidate.startswith("delivery-"):
                    continuation_id = candidate
                    break
                time.sleep(0.1)
            if not continuation_id:
                reason = ((duty or {}).get("observation") or {}).get("reason") or "no-continuation-recorded"
                raise ReplayBlocked("sessionstart-native-continuation-unobserved:" + str(reason))
            continuation_deadline = time.monotonic() + 240
            continuation_turn = None
            all_turns = []
            while time.monotonic() < continuation_deadline:
                all_turns = _all_turns(endpoint, thread_id)
                continuation_turn = _turn_for_client_message(all_turns, continuation_id)
                if continuation_turn and continuation_turn.get("status") in {"completed", "interrupted"}:
                    break
                time.sleep(0.1)
            if continuation_turn is None:
                raise ReplayBlocked("stable-native-continuation-not-consumed")
            if continuation_turn.get("status") != "completed":
                raise ReplayBlocked("native-continuation-turn-interrupted")
            continuation_messages, continuation_tools = _turn_items(continuation_turn)
            _write_json(output_dir / "continuation-turn.json", continuation_turn)
            continuation_occurrences = sum(1 for turn in _all_turns(endpoint, thread_id)
                                           if _turn_for_client_message([turn], continuation_id) is not None)
            if continuation_occurrences != 1:
                raise ReplayFailed("stable-native-continuation-not-exactly-once")
            settle_deadline = time.monotonic() + 30
            settled_duty = obligations.get(duty_id)
            while time.monotonic() < settle_deadline:
                settled_duty = obligations.get(duty_id)
                if isinstance(settled_duty, dict) and settled_duty.get("state") == "complete":
                    break
                time.sleep(0.1)
            if not isinstance(settled_duty, dict) or settled_duty.get("state") != "complete":
                raise ReplayBlocked("completed-native-receiving-duty-not-settled")
            _append_event(output_dir / "native-events.jsonl", {
                "event": "completed-native-receiving-duty-settled",
                "duty_id": duty_id, "delivery_id": completion_record["delivery_id"],
                "receiving_turn_id": continuation_turn.get("id"),
                "duty_state": settled_duty.get("state"),
                "attempt_row_status": "done",
            })
            final_text = continuation_messages[-1] if continuation_messages else ""
            tool_trace = [*initial_tools, *continuation_tools]
            (output_dir / "turn-output.txt").write_text(final_text + "\n", encoding="utf-8")
            (output_dir / "tool-trace.jsonl").write_text("".join(
                json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in tool_trace),
                encoding="utf-8")
            report_check = _first_report_checks(final_text, tool_trace, fixture)
            original_ack_identity = pending_delivery.received_ack_identity(
                first_record, thread_id, completion_record["delivery_id"])
            followthrough_after_first_report, followthrough_stat_after_first_report = _file_snapshot(
                followthrough)
            _write_json(output_dir / "first-report-facts.json", fixture["facts"]["report"])
            _write_json(output_dir / "first-report-checks.json", report_check)
            d1_checks = {key: value for key, value in report_check.items()
                         if isinstance(value, bool)}
            _run_replay_case_sequence(report, output_dir, [{
                "case_id": "d1_first_report", "observe": lambda: d1_checks,
                "evidence": ["continuation-turn.json", "turn-output.txt", "tool-trace.jsonl",
                             "first-report-facts.json", "first-report-checks.json"],
            }])
            if not _case_can_continue(report["replay_cases"]["d1_first_report"]):
                raise ReplayBlocked("first-report-case-has-no-judgment")
            _append_event(output_dir / "native-events.jsonl", {
                "event": "first-closing-report-validated", "turn_id": continuation_turn.get("id"),
                "case_status": report["replay_cases"]["d1_first_report"]["status"],
                "case_reason": report["replay_cases"]["d1_first_report"]["reason"],
                "report_matches_exact_facts": report_check["report_matches_fixture_facts"],
                "all_exact_artifacts_read": report_check["every_exact_artifact_was_read"],
                "verifier_tool_trace_passed": report_check["fixture_verifier_passed_in_native_tool_trace"],
                "one_time_followthrough_observed": report_check["followthrough_state_matches_one_time_fixture_action"],
                "fixture_id": fixture["facts"]["fixture_id"],
            })

            # Re-expose the late cleanup and same exact result only inside the
            # isolated ledger. The completed result is ACKed; cleanup remains
            # with its controller and must not become another model turn.
            history_before_cleanup = _all_turns(endpoint, thread_id)
            cleanup_notice = dispatch_supervision.materialize(
                jobs, {attempt}, reason="join-deadline")[0]
            cleanup_text = dispatch_session_sweep._bounded_receipt_text(cleanup_notice)
            cleanup_is_current = dispatch_supervision.notice_is_current(cleanup_notice)
            _append_event(output_dir / "native-events.jsonl", {
                "event": "late-cleanup-materialized", "delivery_id": cleanup_notice["delivery_id"],
                "receipt_digest": cleanup_notice["receipt_digest"],
                "obligation_revision": (cleanup_notice.get("receipt") or {}).get("obligation_revision"),
                "timestamp_provenance": cleanup_notice.get("timestamp_provenance"),
                "rendered_context": cleanup_text,
            })
            _append_event(output_dir / "native-events.jsonl", {
                "event": "late-join-deadline-observed", "delivery_id": cleanup_notice["delivery_id"],
                "reason": (cleanup_notice.get("receipt") or {}).get("reason"),
                "cleanup_only": True, "synthetic_cleanup_state": "unknown",
            })
            duplicate = pending_delivery.create(runtime,
                recipient_kind="codex-native-queue", recipient_key=thread_id,
                delivery_id=completion_record["delivery_id"], session_generation="",
                session_generation_supported="0", attempt_ids=[attempt], parent_attempt_id="-",
                route_id=route_id, route_node="_owner", receipt=receipt,
                receipt_digest=completion_record["receipt_digest"],
                row_revisions={attempt: metadata["delivery_row_revision"]})
            ack_after_duplicate = pending_delivery.read(
                runtime, thread_id, completion_record["delivery_id"])
            duplicate_kept_exact_ack = (
                duplicate.get("state") == "acked" and ack_after_duplicate is not None
                and pending_delivery.received_ack_identity(
                    ack_after_duplicate, thread_id, completion_record["delivery_id"])
                    == original_ack_identity)
            _append_event(output_dir / "native-events.jsonl", {
                "event": "same-result-redelivery", "delivery_id": duplicate["delivery_id"],
                "receipt_digest": duplicate["receipt_digest"], "state": duplicate["state"],
                "ack_identity": original_ack_identity,
            })
            # Exercise the same native courier that will deliver a later
            # conflict. Its real validator must reject this resolved cleanup
            # before the queue transport can see it.
            spec = importlib.util.spec_from_file_location(
                "codex_parent_replay_managed_completion", UTILITIES / "codex-managed-completion.py")
            if spec is None or spec.loader is None:
                raise ReplayBlocked("native-notice-courier-unavailable")
            managed_completion = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(managed_completion)
            watcher_args = SimpleNamespace(
                jobs=jobs.resolve(), parent_session_id=thread_id,
                sealed_batch_id=metadata["managed_sealed_batch_id"],
                interval=0.05, queue_socket=endpoint,
            )
            watcher = managed_completion.NoticeWatcher(watcher_args, {attempt}, capability=None)
            cleanup_path = pending_delivery.record_path(runtime, thread_id, cleanup_notice["delivery_id"])
            watcher._one(cleanup_path)
            cleanup_record_after_cleanup = pending_delivery.read(
                runtime, thread_id, cleanup_notice["delivery_id"])
            history_after_cleanup = _all_turns(endpoint, thread_id)
            queue_after_cleanup = codex_queue_delivery.list_queue(endpoint, thread_id, timeout=10)
            cleanup_turn_occurrences = sum(
                1 for turn in history_after_cleanup
                if _turn_for_client_message([turn], cleanup_notice["delivery_id"]) is not None)
            no_cleanup_turn = cleanup_turn_occurrences == 0
            no_extra_turn = (len(history_after_cleanup) == len(history_before_cleanup)
                             and no_cleanup_turn)
            no_cleanup_queue_item = not any(
                item.get("clientUserMessageId") == cleanup_notice["delivery_id"]
                for item in queue_after_cleanup)
            no_queued_duplicate = not any(
                item.get("clientUserMessageId") == completion_record["delivery_id"]
                for item in queue_after_cleanup)
            old_after_cleanup = pending_delivery.read(
                runtime, thread_id, completion_record["delivery_id"])
            old_ack_preserved_after_cleanup = (
                old_after_cleanup is not None
                and pending_delivery.received_ack_identity(
                    old_after_cleanup, thread_id, completion_record["delivery_id"])
                    == original_ack_identity)
            cleanup_courier_check = {
                "delivery_id": cleanup_notice["delivery_id"],
                "state": cleanup_record_after_cleanup.get("state"),
                "reason": cleanup_record_after_cleanup.get("expiry_reason"),
                "old_ack_identity_preserved": old_ack_preserved_after_cleanup,
                "cleanup_native_turn_occurrences": cleanup_turn_occurrences,
                "cleanup_native_queue_items": sum(
                    1 for item in queue_after_cleanup
                    if item.get("clientUserMessageId") == cleanup_notice["delivery_id"]),
                "no_additional_native_turn": no_extra_turn,
                "no_duplicate_completion_queue_item": no_queued_duplicate,
            }
            _write_json(output_dir / "late-cleanup-courier.json", cleanup_courier_check)
            d2_observation = {
                "cleanup_is_current": cleanup_is_current,
                "cleanup_text": cleanup_text,
                "duplicate_state": duplicate.get("state"),
                "duplicate_kept_exact_ack": duplicate_kept_exact_ack,
                "cleanup_courier_state": cleanup_record_after_cleanup.get("state"),
                "cleanup_courier_reason": cleanup_record_after_cleanup.get("expiry_reason"),
                "old_ack_preserved": old_ack_preserved_after_cleanup,
                "no_extra_turn": no_extra_turn,
                "no_cleanup_queue_item": no_cleanup_queue_item,
                "no_queued_duplicate": no_queued_duplicate,
            }
            _run_replay_case_sequence(report, output_dir, [{
                "case_id": "d2_duplicate_cleanup", "observe": lambda: d2_observation,
                "evidence": ["late-cleanup-courier.json", "native-events.jsonl"],
            }])
            _append_event(output_dir / "native-events.jsonl", {
                "event": "late-cleanup-rejected-by-native-courier",
                "case_status": report["replay_cases"]["d2_duplicate_cleanup"]["status"],
                "case_reason": report["replay_cases"]["d2_duplicate_cleanup"]["reason"],
                **cleanup_courier_check,
                "courier": "codex-managed-completion.NoticeWatcher._one",
            })

            # A genuinely new terminal conflict gets a new notice revision.
            # It must reach the still-idle native parent through the existing
            # Codex managed-completion NoticeWatcher and queue courier.
            late_metadata = {
                **metadata, "cleanup_state": "unknown", "terminal_conflict": "1",
                "conflicting_terminal_note": "synthetic-late-failure",
                "conflicting_failure_class": "fail",
                "conflicting_classifier_source": "native-replay-late-fixture",
            }
            jobs.write_text(
                "2026-10-19T00:00:00Z\tdone\t" + str(worktree) + "\t" + str(worktree)
                + "\tisolated-parent-replay\t"
                + ",".join(f"{key}={value}" for key, value in late_metadata.items()) + "\n",
                encoding="utf-8")
            from dispatch_attempt_policy import terminal_conflict_pending
            from dispatch_supervision import _rows as supervision_rows
            late_row = supervision_rows(jobs, {attempt})[attempt]
            conflict_fixture_valid = terminal_conflict_pending(late_row[1]) and late_row[0] == "done"
            late_notice = dispatch_supervision.materialize(
                jobs, {attempt}, reason="terminal-evidence-conflict")[0]
            conflict_notice_is_distinct_current = (
                late_notice["delivery_id"] != completion_record["delivery_id"]
                and late_notice["delivery_id"] != cleanup_notice["delivery_id"]
                and dispatch_supervision.notice_is_current(late_notice))
            _write_json(output_dir / "late-cleanup-notice.json", cleanup_notice)
            _write_json(output_dir / "late-conflict-notice.json", late_notice)
            _append_event(output_dir / "native-events.jsonl", {
                "event": "new-conflict-notice-materialized-after-old-ack",
                "old_ack_delivery_id": completion_record["delivery_id"],
                "old_ack_identity": original_ack_identity,
                "new_notice_delivery_id": late_notice["delivery_id"],
                "new_notice_receipt_digest": late_notice["receipt_digest"],
                "new_notice_reason": late_notice["receipt"]["reason"],
                "new_notice_revision": late_notice["receipt"]["obligation_revision"],
                "new_conflicting_terminal_note": late_metadata["conflicting_terminal_note"],
                "old_goal_row_status": late_row[0],
            })

            watcher.start()
            _append_event(output_dir / "native-events.jsonl", {
                "event": "idle-parent-native-notice-courier-started",
                "thread_id": thread_id, "courier": "codex-managed-completion.NoticeWatcher",
                "manual_prompt_or_wake": False,
            })
            late_notice_deadline = time.monotonic() + 180
            late_notice_turn = None
            late_notice_record = None
            try:
                while time.monotonic() < late_notice_deadline:
                    late_notice_record = pending_delivery.read(
                        runtime, thread_id, late_notice["delivery_id"])
                    late_notice_turn = _turn_for_client_message(
                        _all_turns(endpoint, thread_id), late_notice["delivery_id"])
                    if (late_notice_record and late_notice_record.get("state") == "acked"
                            and late_notice_turn and late_notice_turn.get("status")
                            in {"completed", "interrupted"}):
                        break
                    time.sleep(0.05)
            finally:
                watcher.close()
            late_notice_ack_observed = (
                late_notice_record is not None and late_notice_record.get("state") == "acked")
            late_notice_status = late_notice_turn.get("status") if late_notice_turn else None
            if late_notice_status not in {None, "completed", "interrupted"}:
                _run_replay_case_sequence(report, output_dir, [{
                    "case_id": "d3_new_conflict", "skip_evaluation": True, "status": "BLOCKED",
                    "reason": "new conflict turn remains active; independent question is unsafe",
                    "evidence": ["native-events.jsonl"],
                }])
                raise ReplayBlocked("idle-parent-new-notice-turn-still-active")
            late_notice_turn = late_notice_turn or {}
            late_notice_messages, late_notice_tools = _turn_items(late_notice_turn)
            _write_json(output_dir / "idle-parent-notice-turn.json", late_notice_turn)
            late_notice_ack_identity = (pending_delivery.received_ack_identity(
                late_notice_record, thread_id, late_notice["delivery_id"])
                if late_notice_ack_observed else None)
            _write_json(output_dir / "idle-parent-notice-ack.json", late_notice_ack_identity)
            (output_dir / "idle-parent-notice-output.txt").write_text(
                (late_notice_messages[-1] if late_notice_messages else "") + "\n", encoding="utf-8")
            (output_dir / "idle-parent-notice-tool-trace.jsonl").write_text("".join(
                json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
                for item in late_notice_tools), encoding="utf-8")
            idle_turns = _all_turns(endpoint, thread_id)
            notice_occurrences = sum(
                1 for turn in idle_turns
                if _turn_for_client_message([turn], late_notice["delivery_id"]) is not None)
            cleanup_turn_occurrences = sum(
                1 for turn in idle_turns
                if _turn_for_client_message([turn], cleanup_notice["delivery_id"]) is not None)
            idle_response_count = len(idle_turns) - len(history_after_cleanup)
            followthrough_after_idle_notice, followthrough_stat_after_idle_notice = _file_snapshot(
                followthrough)
            late_notice_action_invocations = len(_script_invocations(late_notice_tools, fixture["action"]))
            no_duplicate_idle_action_or_response = (
                cleanup_turn_occurrences == 0 and idle_response_count == 1
                and notice_occurrences == 1 and late_notice_action_invocations == 0
                and followthrough_after_first_report is not None
                and followthrough_after_idle_notice == followthrough_after_first_report
                and followthrough_stat_after_idle_notice == followthrough_stat_after_first_report)
            old_after_notice = pending_delivery.read(
                runtime, thread_id, completion_record["delivery_id"])
            old_ack_preserved_after_notice = (
                old_after_notice is not None
                and pending_delivery.received_ack_identity(
                    old_after_notice, thread_id, completion_record["delivery_id"])
                    == original_ack_identity)
            _append_event(output_dir / "native-events.jsonl", {
                "event": "idle-parent-new-notice-consumed",
                "new_notice_delivery_id": late_notice["delivery_id"],
                "new_notice_turn_id": late_notice_turn.get("id"),
                "new_notice_ack_state": late_notice_record.get("state") if late_notice_record else None,
                "new_notice_turn_status": late_notice_turn.get("status"),
                "new_notice_turn_occurrences": notice_occurrences,
                "late_cleanup_turn_occurrences": cleanup_turn_occurrences,
                "idle_parent_new_turn_count": idle_response_count,
                "no_duplicate_action_or_response": no_duplicate_idle_action_or_response,
                "old_ack_delivery_id": completion_record["delivery_id"],
                "old_ack_identity_preserved": old_ack_preserved_after_notice,
                "native_turn_count_after_notice": len(idle_turns),
            })
            d3_observation = {
                "conflict_fixture_valid": conflict_fixture_valid,
                "notice_is_distinct_current": conflict_notice_is_distinct_current,
                "notice_ack_observed": late_notice_ack_observed,
                "notice_turn_status": late_notice_status,
                "notice_has_response": bool(late_notice_messages),
                "notice_occurrences": notice_occurrences,
                "old_ack_preserved": old_ack_preserved_after_notice,
                "no_duplicate_action_or_response": no_duplicate_idle_action_or_response,
            }
            d3_blocker = None
            if not late_notice_ack_observed:
                d3_blocker = "new conflict exact native ACK was not observed"
            elif late_notice_status != "completed":
                d3_blocker = "new conflict native turn did not complete"
            elif followthrough_after_first_report is None:
                d3_blocker = "follow-through state absent before new notice; no-repeat comparison unavailable"
            _run_replay_case_sequence(report, output_dir, [{
                "case_id": "d3_new_conflict", "observe": lambda: d3_observation,
                "status": "BLOCKED" if d3_blocker else None, "reason": d3_blocker,
                "evidence": ["late-conflict-notice.json", "idle-parent-notice-turn.json",
                             "idle-parent-notice-ack.json", "idle-parent-notice-tool-trace.jsonl",
                             "native-events.jsonl"],
            }])

            followthrough_before_question, stat_before_question = _file_snapshot(followthrough)
            question = ("Did this isolated fixture deploy to production? Answer directly from the exact result. "
                        "Do not repeat the fixture follow-through.")
            (output_dir / "direct-question.txt").write_text(question + "\n", encoding="utf-8")
            question_started = _request(endpoint, "turn/start", {
                "threadId": thread_id,
                "input": [{"type": "text", "text": question, "text_elements": []}],
                "cwd": str(worktree), "approvalPolicy": "never",
                "sandboxPolicy": {"type": "workspaceWrite", "writableRoots": [str(worktree)],
                                  "networkAccess": False, "excludeTmpdirEnvVar": False,
                                  "excludeSlashTmp": False},
            })
            question_turn = question_started.get("turn") or {}
            question_turn_id = question_turn.get("id")
            if not isinstance(question_turn_id, str) or not question_turn_id:
                raise ReplayBlocked("direct-question-turn-start-unconfirmed")
            question_deadline = time.monotonic() + 180
            question_record = None
            while time.monotonic() < question_deadline:
                question_record = _turn_record(endpoint, thread_id, question_turn_id)
                if question_record and question_record.get("status") in {"completed", "interrupted"}:
                    break
                time.sleep(0.1)
            if not question_record or question_record.get("status") != "completed":
                raise ReplayBlocked("direct-question-turn-unfinished")
            question_messages, question_tools = _turn_items(question_record)
            _write_json(output_dir / "direct-question-turn.json", question_record)
            direct_answer = question_messages[-1] if question_messages else ""
            followthrough_after_question, stat_after_question = _file_snapshot(followthrough)
            question_action_invocations = len(_script_invocations(question_tools, fixture["action"]))
            followthrough_unchanged = (
                followthrough_before_question is not None
                and followthrough_after_question == followthrough_before_question
                and stat_after_question == stat_before_question
                and question_action_invocations == 0)
            (output_dir / "direct-answer.txt").write_text(direct_answer + "\n", encoding="utf-8")
            question_blocker = ("follow-through state absent before question; no-repeat comparison unavailable"
                                if followthrough_before_question is None else None)
            question_observation = {"answer": direct_answer,
                                    "followthrough_unchanged": followthrough_unchanged,
                                    "action_invocations": question_action_invocations}
            question_checks = _evaluate_replay_case("direct_question", question_observation)
            direct_answer_ok = question_checks["answers_from_exact_production_status"]
            _run_replay_case_sequence(report, output_dir, [{
                "case_id": "direct_question", "observe": lambda: question_observation,
                "status": "BLOCKED" if question_blocker else None, "reason": question_blocker,
                "evidence": ["direct-question.txt", "direct-question-turn.json", "direct-answer.txt"],
            }])
            with (output_dir / "tool-trace.jsonl").open("a", encoding="utf-8") as stream:
                for item in question_tools:
                    stream.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
            cleanup_record_after_question = pending_delivery.read(
                runtime, thread_id, cleanup_notice["delivery_id"])
            all_turns_after_question = _all_turns(endpoint, thread_id)
            queue_after_question = codex_queue_delivery.list_queue(endpoint, thread_id, timeout=10)
            latest_ack = pending_delivery.read(runtime, thread_id, completion_record["delivery_id"])
            latest_conflict_row = supervision_rows(jobs, {attempt})[attempt]
            conflict_still_pending = terminal_conflict_pending(latest_conflict_row[1])
            conflict_ack = pending_delivery.read(runtime, thread_id, late_notice["delivery_id"])
            conflict_turn_occurrences = sum(
                1 for turn in all_turns_after_question
                if _turn_for_client_message([turn], late_notice["delivery_id"]) is not None)
            prior_d3 = report["replay_cases"]["d3_new_conflict"]
            final_d3_observation = {
                **d3_observation,
                "new_failure_conflict_remains_visible": conflict_still_pending,
                "original_ack_identity_still_preserved": bool(latest_ack)
                    and pending_delivery.received_ack_identity(
                        latest_ack, thread_id, completion_record["delivery_id"])
                        == original_ack_identity,
            }
            final_d3_checks = _evaluate_replay_case("d3_new_conflict", final_d3_observation)
            _run_replay_case_sequence(report, output_dir, [{
                "case_id": "d3_new_conflict", "observe": lambda: final_d3_observation,
                "status": prior_d3["status"] if prior_d3["status"] == "BLOCKED" else None,
                "reason": prior_d3["reason"] if prior_d3["status"] == "BLOCKED" else None,
                "evidence": ["late-conflict-notice.json", "idle-parent-notice-turn.json",
                             "idle-parent-notice-ack.json", "native-events.jsonl"],
            }])
            report_checks_passed = all(value for value in report_check.values()
                                       if isinstance(value, bool))
            acceptance_checks = {
                "first_report_matches_fixture_and_real_tool_evidence": report_checks_passed,
                "exact_original_ack_identity_preserved": bool(latest_ack)
                    and pending_delivery.received_ack_identity(
                        latest_ack, thread_id, completion_record["delivery_id"])
                        == original_ack_identity,
                "late_cleanup_and_deadline_suppressed_as_duplicate_work": (
                    cleanup_record_after_question is not None
                    and cleanup_record_after_question.get("state") == "rejected"
                    and cleanup_record_after_question.get("expiry_reason") == "supervision-resolved"
                    and old_ack_preserved_after_cleanup and no_extra_turn and no_cleanup_turn
                    and no_cleanup_queue_item and no_queued_duplicate),
                "same_result_redelivery_did_not_reopen_completed_attempt": (
                    duplicate.get("state") == "acked" and late_row[0] == "done"
                    and isinstance(settled_duty, dict) and settled_duty.get("state") == "complete"),
                "new_conflict_notice_delivered_and_consumed_once_while_parent_idle": (
                    conflict_ack is not None and conflict_ack.get("state") == "acked"
                    and conflict_turn_occurrences == 1 and late_notice_turn.get("status") == "completed"
                    and no_duplicate_idle_action_or_response),
                "new_failure_conflict_remains_visible_after_notice": conflict_still_pending,
                "direct_status_answer_did_not_repeat_followthrough": direct_answer_ok and followthrough_unchanged,
            }
            observed_failure = _has_failed_case(report)
            overall_status = _aggregate_replay_status(report, acceptance_checks)
            failed_acceptance_checks = [name for name, passed in acceptance_checks.items() if not passed]
            overall_reason = ("all checks passed" if overall_status == "PASS" else
                              "observed failure in " + observed_failure["case_id"]
                              if overall_status == "FAIL" and observed_failure else
                              "required judgment unavailable: " + ", ".join(failed_acceptance_checks))
            _record_case(report, output_dir, "overall_acceptance", acceptance_checks,
                         status=overall_status, reason=overall_reason, evidence=[
                             "first-report-checks.json", "late-cleanup-courier.json",
                             "idle-parent-notice-turn.json", "direct-question-turn.json",
                         ])
            # The exact history/queue checks are native Codex evidence; receipt
            # identity, conflict state, and cleanup classification are also
            # tied to the same isolated registry and private Codex queue.
            report.update({
                "status": "observed", "thread_id": thread_id, "turn_id": turn_id,
                "original_turn_status_after_crash": first_turn_before_crash.get("status"),
                "continuation_id": continuation_id,
                "continuation_turn_id": continuation_turn.get("id"),
                "duplicate_sessionstart_reconnects": 2,
                "continuation_message_occurrences": sum(
                    1 for turn in all_turns_after_question
                    if _turn_for_client_message([turn], continuation_id) is not None),
                "crash_window_before_model_output_captured": not initial_messages and not initial_tools,
                "attempt_id": attempt, "delivery_id": completion_record["delivery_id"],
                "receipt_digest": completion_record["receipt_digest"],
                "first_turn_status": "interrupted",
                "precrash_tool_count": len(initial_tools),
                "continuation_tool_count": len(continuation_tools),
                "continuation_report_count": len(continuation_messages),
                "report_checks": report_check,
                "first_report_matches_exact_facts": report_checks_passed,
                "original_ack_identity": original_ack_identity,
                "late_cleanup_rendered_cleanup_only": "retains cleanup observation only" in cleanup_text,
                "late_cleanup_additional_native_turns_before_direct_question":
                    max(0, len(history_after_cleanup) - len(history_before_cleanup)),
                "late_cleanup_duplicate_completion_queue_items": sum(
                    1 for item in queue_after_question
                    if item.get("clientUserMessageId") == completion_record["delivery_id"]),
                "duplicate_record_kept_ack_state": duplicate.get("state") == "acked",
                "native_history_count_unchanged_before_direct_question": no_extra_turn,
                "native_queue_has_no_completion_duplicate": no_queued_duplicate,
                "late_cleanup_native_turn_occurrences": cleanup_turn_occurrences,
                "late_cleanup_native_queue_items": cleanup_courier_check["cleanup_native_queue_items"],
                "late_cleanup_rejection_reason": cleanup_record_after_question.get("expiry_reason"),
                "cleanup_notice_retired_as_resolved": bool(
                    cleanup_record_after_question and cleanup_record_after_question.get("state") == "rejected"
                    and cleanup_record_after_question.get("expiry_reason") == "supervision-resolved"),
                "late_conflict_delivery_id": late_notice["delivery_id"],
                "late_conflict_receipt_digest": late_notice["receipt_digest"],
                "late_conflict_obligation_revision": late_notice["receipt"]["obligation_revision"],
                "late_conflict_turn_id": late_notice_turn.get("id"),
                "late_conflict_turn_occurrences": conflict_turn_occurrences,
                "late_conflict_still_pending": conflict_still_pending,
                "attempt_remains_done_after_duplicate": late_row[0] == "done",
                "direct_question_answered": direct_answer_ok,
                "followthrough_unchanged_after_direct_question": followthrough_unchanged,
                "acceptance_checks": acceptance_checks,
                "crash_reconnect_replayed": True,
                "synthetic_fixture_facts": fixture["facts"],
                "limitations": ["Chronology, commit, deployment, serving, independent review, late conflict, "
                    "and follow-through are explicitly synthetic fixture data, not production events.",
                    "The app-server replay used an isolated persisted thread and copied private auth material "
                    "that was removed before model tools ran.",
                    "This native Codex result does not establish Claude/OpenCode parity, PR readiness, CI, or "
                    "the later report/test gates."],
            })
            if overall_status == "FAIL":
                report["status"] = "failed"
                report["verdict"] = "FAIL"
                report["failure_reason"] = "measured-native-parent-acceptance-check-failed: " + ", ".join(
                    name for name, passed in acceptance_checks.items() if not passed)
            elif overall_status == "BLOCKED":
                report.update(status="blocked", verdict="BLOCKED",
                              blocker="required native case judgment unavailable: "
                              + ", ".join(failed_acceptance_checks))
            else:
                report.update(status="observed", verdict="PASS", blocker="none",
                    d1="PASS: exact native ACK, crash interruption, stable continuation, factual first report, one-time follow-through",
                    d2="PASS: late cleanup/deadline and exact same-result redelivery were suppressed without reopening the done attempt",
                    d3="PASS: new post-ACK conflict revision reached and completed on the idle parent through NoticeWatcher/native queue")
            return report
        finally:
            _stop_server(server)


def _run_replay(output_dir: Path) -> dict:
    """Run the replay and durably finish any cases left by a runtime error."""
    try:
        report = _run_replay_impl(output_dir)
        _write_json(output_dir / "replay.json", report)
        return report
    except ReplayFailed as exc:
        kind = "FAIL"
        reason = str(exc)
        detail = None
    except ReplayBlocked as exc:
        kind = "BLOCKED"
        reason = str(exc)
        detail = None
    except Exception as exc:  # preserve the exact runtime limitation in evidence
        kind = "BLOCKED"
        reason = str(exc) or type(exc).__name__
        detail = repr(exc)
    try:
        report = json.loads((output_dir / "replay.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report = {"schema_version": 1, "replay_cases": {}}
    report = _finish_after_runtime_error(report, output_dir, kind, reason, detail)
    _write_json(output_dir / "replay.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args(argv)
    output = args.output_dir.expanduser().resolve()
    try:
        output.mkdir(mode=0o700, parents=True, exist_ok=True)
        report = _run_replay(output)
    except ReplayFailed as exc:
        try:
            report = json.loads((output / "replay.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = {"schema_version": 1}
        report = _finish_after_runtime_error(report, output, "FAIL", str(exc))
    except ReplayBlocked as exc:
        try:
            report = json.loads((output / "replay.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = {"schema_version": 1}
        report = _finish_after_runtime_error(report, output, "BLOCKED", str(exc))
    except Exception as exc:  # noqa: BLE001 -- retain the exact isolated runtime limitation.
        try:
            report = json.loads((output / "replay.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = {"schema_version": 1}
        report = _finish_after_runtime_error(report, output, "BLOCKED",
                                             "isolated-replay-runtime-error", str(exc))
    _write_json(output / "replay.json", report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("verdict") in {"PASS", "BLOCKED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
