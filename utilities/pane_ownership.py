"""Live pane ownership; inherited pane labels are lookup hints, never proof.

No state is written. Codex daemon attachment is proven through the thread's
``codex_tui`` listener PID and the pane's foreground process. In particular,
Claude's parkedJobId names the job originally backgrounded, not necessarily the
conversation currently attached to that frontend.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time


# Every entry (hook, CLI script, import) resolves `fleet.collectors` here, not
# only after `_native_session` has run. Appended so `tools/` never shadows a
# caller's own modules.
_TOOLS_DIR = str(Path(__file__).resolve().parents[1] / "tools")
if _TOOLS_DIR not in sys.path:
    sys.path.append(_TOOLS_DIR)


_THREAD_ID_RE = re.compile(
    r"(?<![0-9A-Za-z-])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9A-Za-z-])"
)
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[()][A-Za-z0-9])"
)
_CODEX_LOCATION_MENU = (
    "whereshouldthenewconversationrun", "whereshouldtheforkedconversationrun",
    "usecurrentgitworktree", "keepusingthecurrentworkingdirectory", "createnewgitworktree",
)
_CODEX_FOOTER_HINT = ("?forshortcuts",)
_WORKER_ENV = ("AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_DEPTH", "OPENCODE_DISPATCH_SLUG",
               "FLEET_TITLE_REFRESH", "MEM_DISTILL")


def codex_footer_threads(lines, *, current_id_only=False):
    """Return thread UUIDs from the visible Codex footer.

    Current identity is the whole UUID in the last native footer field, after a
    composer/status/help-row layout. A hidden footer or a conversation-location
    menu is unknown. Broad discovery remains available to non-ownership readers.
    """
    if not isinstance(lines, (list, tuple)):
        return set()
    cleaned = [_ANSI_ESCAPE_RE.sub("", line).replace("\r", "").replace("\u00a0", " ")
               if isinstance(line, str) else "" for line in lines]
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()
    prompt = next((i for i in range(len(cleaned) - 1, -1, -1)
                   if cleaned[i].lstrip().startswith("›")), None)
    if prompt is None:
        return set()
    if current_id_only:
        tail = [line.strip() for line in cleaned[prompt + 1:] if line.strip()]
        flat = "".join("".join(cleaned[prompt + 1:]).split()).lower()
        if (len(tail) != 2 or any(token in flat for token in _CODEX_LOCATION_MENU)
                or not all(token in "".join(tail[-1].lower().split()) for token in _CODEX_FOOTER_HINT)):
            return set()
        status = tail[0]
        last_field = status.rsplit("·", 1)[-1].strip()
        match = _THREAD_ID_RE.fullmatch(last_field)
        return {match.group(0)} if match else set()
    return {match.group(0) for line in cleaned[prompt + 1:]
            for match in _THREAD_ID_RE.finditer(line)}


def _herdr_env(socket_path):
    """Bind a Herdr read to a supplied endpoint; ``""`` explicitly means default."""
    if socket_path is None:
        return None
    env = dict(os.environ)
    env.pop("HERDR_PANE_ID", None)
    if socket_path:
        env["HERDR_SOCKET_PATH"] = str(socket_path)
    else:
        env.pop("HERDR_SOCKET_PATH", None)
    return env


def _read_codex_footer_threads(pane, *, executable=None, socket_path=None):
    """Read one pane's visible screen and parse only its native Codex footer."""
    executable = executable or shutil.which("herdr")
    if not executable:
        return None
    env = _herdr_env(socket_path)
    try:
        result = subprocess.run(
            [executable, "agent", "read", str(pane), "--source", "visible", "--format", "ansi"],
            capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL,
            timeout=0.5, check=False, env=env,
        )
        if result.returncode or len(result.stdout.encode("utf-8")) > 131072:
            return None
        visible = _ANSI_ESCAPE_RE.sub("", result.stdout).replace("\r", "")
        return codex_footer_threads(visible.splitlines(), current_id_only=True)
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None


def _registered_worker(pid):
    """Whether this candidate frontend carries the existing registered-worker markers."""
    try:
        raw = Path(f"/proc/{int(pid)}/environ").read_bytes()
    except (OSError, ValueError, TypeError):
        return True
    env = {}
    for item in raw.split(b"\0"):
        key, sep, value = item.partition(b"=")
        if sep:
            env[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return (env.get("AGENT_SESSION_ROLE") == "worker"
            or any(env.get(name) for name in _WORKER_ENV))


def _process(pid):
    try:
        pid = int(pid)
        path = Path(f"/proc/{pid}")
        fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] in {"Z", "X"} or path.stat().st_uid != os.getuid():
            return None
        return (pid, int(fields[1]), int(fields[2]), int(fields[4]),
                int(fields[5]), fields[19])  # pid, parent, pgid, tty, tpgid, start
    except (OSError, ValueError, IndexError, TypeError):
        return None


def _native_claude_executable(command):
    return Path(command).name == "claude" or "/claude/versions/" in command


def _runtime(pid):
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        words = [part.decode("utf-8", "replace") for part in argv if part]
        name = Path(words[0]).name
        # Native daemon children rewrite argv[0] to a process title. They
        # remain service-side processes, never physical pane owners.
        if name in {"claude bg-spare", "claude bg-pty-host"}:
            return "claude", True
        if _native_claude_executable(words[0]):
            return "claude", any(w in {"daemon", "bg-pty-host", "bg-spare"} for w in words[1:3])
        if name == "codex" or name.startswith("codex-"):
            if name != "codex":
                return "codex", True
            # Codex accepts global options before the command. Skip each
            # option and its value so a config value containing "app-server"
            # cannot turn a TUI into a service, or hide the real command.
            value_options = {"-c", "--config", "--profile", "--ask-for-approval",
                             "-a", "--sandbox", "-s", "--cd", "-C", "--add-dir",
                             "--model", "-m"}
            index = 1
            command = ""
            while index < len(words):
                word = words[index]
                if word in value_options:
                    index += 2
                    continue
                if any(word.startswith(option + "=") for option in value_options):
                    index += 1
                    continue
                if word == "--oss":
                    index += 1
                    continue
                if word.startswith("-"):
                    index += 1
                    continue
                command = word
                break
            return "codex", command == "app-server"
        if name == "opencode":
            return "opencode", any(w in {"serve", "web"} for w in words[1:3])
    except (OSError, IndexError):
        pass
    return "", False


def codex_caller_binding(session_id, *, pid=None, environ=None, executable=None):
    """Return the one proved TUI binding for an interactive Codex daemon caller.

    Inherited Herdr values are intentionally ignored. The runtime's own thread
    id selects the binding; workers, service identities and ambiguous panes
    fail closed.
    """
    env = os.environ if environ is None else environ
    if (not _THREAD_ID_RE.fullmatch(session_id or "")
            or env.get("CODEX_THREAD_ID") != session_id):
        return None
    caller_pid = os.getpid() if pid is None else int(pid)
    if _registered_worker(caller_pid):
        return None
    caller, service = _caller_runtime(caller_pid, "codex")
    if caller is None or not service:
        return None
    bindings = codex_tui_process_bindings(session_id, pane_hint=None, executable=executable)
    if len(bindings) != 1:
        return None
    binding = bindings[0]
    if not isinstance(binding, dict) or not all(
            binding.get(key) for key in ("pid", "pid_start", "session_id", "pane")):
        return None
    if binding.get("session_id") != session_id or _registered_worker(binding["pid"]):
        return None
    record = _process(binding["pid"])
    if not record or str(record[5]) != str(binding["pid_start"]):
        return None
    return dict(binding)


def _caller_runtime(pid, harness):
    for _ in range(32):
        record = _process(pid)
        if not record or record[0] <= 1:
            break
        kind, service = _runtime(pid)
        if kind:
            return (record, service) if kind == harness else (None, False)
        pid = record[1]
    return None, False


def _native_session(pid, harness, *, codex_observation=None):
    """Exact native evidence only; no same-directory/start-time rollout guesses."""
    tools = str(Path(__file__).resolve().parents[1] / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    try:
        if harness == "claude":
            from fleet.collectors import claude
            record = claude.read_registry(pid) or {}
            live = _process(pid)
            if live and str(record.get("procStart") or "") == live[5]:
                # A parked frontend still names its original conversation.
                if record.get("parkedJobId"):
                    return ""
                return record.get("sessionId") or ""
        elif harness == "codex":
            from fleet.process_identity import PROVEN, process_identity
            found = process_identity(pid, "codex", codex_observation=codex_observation)
            return found.session_id if found.confidence == PROVEN else ""
        elif harness == "opencode":
            from fleet.process_identity import process_identity, PROVEN
            found = process_identity(pid, harness)
            return found.session_id if found.confidence == PROVEN else ""
    except Exception:
        pass
    return ""


def _pane_process_info(pane, *, executable, server=None, socket_path=None):
    env = _herdr_env(socket_path)
    command = [executable, *(["--session", server] if server else []),
               "pane", "process-info", "--pane", str(pane)]
    done = subprocess.run(command, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          timeout=0.5, check=False, env=env)
    if done.returncode:
        return None
    info = (json.loads(done.stdout).get("result") or {}).get("process_info") or {}
    return info if info.get("pane_id") == pane else None


def _footer_proves_current_thread(pane, session_id, pid, process_record, group, *,
                                  executable, server=None, socket_path=None):
    """Use two fresh native footer reads to prove the pane's current selection."""
    if not _THREAD_ID_RE.fullmatch(session_id or "") or _registered_worker(pid):
        return False
    if _process(pid) != process_record:
        return False
    first = _read_codex_footer_threads(pane, executable=executable, socket_path=socket_path)
    if first != {session_id}:
        return False
    time.sleep(0.04)
    if _process(pid) != process_record:
        return False
    second = _read_codex_footer_threads(pane, executable=executable, socket_path=socket_path)
    if second != {session_id} or _process(pid) != process_record:
        return False
    try:
        latest = _pane_process_info(pane, executable=executable, server=server,
                                    socket_path=socket_path)
        latest_group = int((latest or {}).get("foreground_process_group_id") or 0)
        latest_pids = {int(item.get("pid")) for item in (latest or {}).get("foreground_processes") or []
                       if isinstance(item, dict) and str(item.get("pid", "")).isdigit()}
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
        return False
    current = _process(pid)
    return bool(latest and latest_group == group and pid in latest_pids
                and current == process_record and current[3] and current[2] == group
                and current[4] == group)


def current_codex_thread(pane, pid=None, *, executable=None, server=None, socket_path=None):
    """Return process_identity's thread when its TUI is foreground in this pane.

    The footer is an optional confirmation. A readable, nonempty footer that
    disagrees with process_identity refuses the pane; an absent or unreadable
    footer does not erase the daemon's PID/socket proof.
    """
    if not pane:
        return ""
    executable = executable or shutil.which("herdr")
    if not executable or pid is not None and _registered_worker(pid):
        return ""
    try:
        info = _pane_process_info(pane, executable=executable, server=server,
                                  socket_path=socket_path)
        group = int((info or {}).get("foreground_process_group_id") or 0)
        foreground_pids = {int(item.get("pid"))
                           for item in (info or {}).get("foreground_processes") or []
                           if isinstance(item, dict) and str(item.get("pid", "")).isdigit()}
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
        return ""
    if not info:
        return ""
    candidates = sorted(foreground_pids if pid is None else foreground_pids & {int(pid)})
    current_ids = set()
    from fleet.collectors import codex as codex_collector
    codex_observation = codex_collector.DaemonIdentityObservation()
    for candidate in candidates:
        record = _process(candidate)
        kind, service = _runtime(candidate)
        if (kind != "codex" or service or not record or not record[3]
                or record[2] != group or record[4] != group or _registered_worker(candidate)):
            continue
        native_sid = _native_session(candidate, "codex", codex_observation=codex_observation)
        if not native_sid:
            continue
        footer = _read_codex_footer_threads(pane, executable=executable,
                                            socket_path=socket_path)
        if isinstance(footer, set) and footer:
            if footer != {native_sid}:
                continue
            if not _footer_proves_current_thread(
                    pane, native_sid, candidate, record, group,
                    executable=executable, server=server, socket_path=socket_path):
                continue
        if (_process(candidate) == record
                and _native_session(candidate, "codex", codex_observation=codex_observation) == native_sid):
            current_ids.add(native_sid)
    return next(iter(current_ids)) if len(current_ids) == 1 else ""


def current_codex_thread_for_caller(pane, *, executable=None, server=None, socket_path=None):
    """Return the caller's exact CODEX_THREAD_ID or its process_identity thread."""
    caller_pid = os.getpid()
    if _registered_worker(caller_pid):
        return None
    own, service = _caller_runtime(caller_pid, "codex")
    if own is None:
        return None
    caller_sid = os.environ.get("CODEX_THREAD_ID", "")
    if _THREAD_ID_RE.fullmatch(caller_sid):
        return caller_sid
    if service:
        return None
    return _native_session(own[0], "codex") or None


def _loader_verified_pane(pane, session_id, owner_pid, owner_start, *,
                          executable, server=None, socket_path=None, codex_observation=None):
    """Confirm a caller-selected thread's exact loader PID in a herdr pane."""
    if _registered_worker(owner_pid):
        return ""
    if _native_session(owner_pid, "codex", codex_observation=codex_observation) != session_id:
        return ""
    record = _process(owner_pid)
    kind, service = _runtime(owner_pid)
    if (not record or kind != "codex" or service or not record[3]
            or record[2] != record[4] or str(record[5]) != str(owner_start)):
        return ""
    try:
        info = _pane_process_info(pane, executable=executable, server=server,
                                  socket_path=socket_path)
        group = int((info or {}).get("foreground_process_group_id") or 0)
        foreground_pids = {int(item.get("pid"))
                           for item in (info or {}).get("foreground_processes") or []
                           if isinstance(item, dict) and str(item.get("pid", "")).isdigit()}
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
        return ""
    if not info or owner_pid not in foreground_pids or record[2] != group or record[4] != group:
        return ""
    footer = _read_codex_footer_threads(pane, executable=executable, socket_path=socket_path)
    if isinstance(footer, set) and footer:
        if footer != {session_id} or not _footer_proves_current_thread(
                pane, session_id, owner_pid, record, group, executable=executable,
                server=server, socket_path=socket_path):
            return ""
    try:
        latest = _pane_process_info(pane, executable=executable, server=server,
                                    socket_path=socket_path)
        latest_group = int((latest or {}).get("foreground_process_group_id") or 0)
        latest_pids = {int(item.get("pid"))
                       for item in (latest or {}).get("foreground_processes") or []
                       if isinstance(item, dict) and str(item.get("pid", "")).isdigit()}
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
        return ""
    return pane if (_process(owner_pid) == record and latest_group == group
                    and owner_pid in latest_pids
                    and _native_session(owner_pid, "codex", codex_observation=codex_observation) == session_id) else ""


def codex_tui_process_bindings(session_id, *, pane_hint=None, executable=None):
    """Return exact live Codex TUI PID/start bindings for one native thread."""
    if not _THREAD_ID_RE.fullmatch(session_id or ""):
        return []
    from fleet.collectors import procscan

    executable = executable or shutil.which("herdr")
    if not executable:
        return []
    try:
        process_ids = [int(item) for item in os.listdir("/proc") if item.isdigit()]
    except OSError:
        return []
    candidates = {}
    from fleet.collectors import codex as codex_collector
    codex_observation = codex_collector.DaemonIdentityObservation()
    for owner_pid in process_ids:
        try:
            kind, service = _runtime(owner_pid)
            record = _process(owner_pid)
            if kind != "codex" or service or not record or _registered_worker(owner_pid):
                continue
            # The pane and endpoint checks read only the environment; the session
            # proof walks every process's fd table, so only the frontends they
            # leave reach it, inside _loader_verified_pane.
            owner_env = procscan.read_environ(owner_pid)
            pane = str(owner_env.get("HERDR_PANE_ID") or "").strip()
            if not pane or pane_hint and pane != pane_hint:
                continue
            # The endpoint travels with the pane. An empty value is an explicit
            # default-endpoint binding, never permission to inherit another socket.
            socket_path = str(owner_env.get("HERDR_SOCKET_PATH") or "")
            if owner_env.get("AGENT_HERDR_SESSION") and not socket_path:
                continue  # a named server without its socket path cannot be carried safely
            if _loader_verified_pane(
                    pane, session_id, owner_pid, record[5], executable=executable,
                    socket_path=socket_path, codex_observation=codex_observation):
                binding = (owner_pid, str(record[5]), pane, socket_path)
                candidates[binding] = {
                    "pid": owner_pid, "pid_start": str(record[5]),
                    "session_id": session_id, "pane": pane,
                    "socket_path": socket_path,
                }
        except Exception:
            continue
    return [candidates[key] for key in sorted(candidates)]


def codex_tui_pane_endpoint(session_id, *, pane_hint=None, executable=None):
    """Return the unique proven ``(pane, socket_path)`` for a current Codex thread."""
    bindings = codex_tui_process_bindings(
        session_id, pane_hint=pane_hint, executable=executable)
    endpoints = {(item["pane"], item["socket_path"]) for item in bindings}
    return next(iter(endpoints)) if len(endpoints) == 1 else None


def codex_tui_processes_for_session(session_id):
    """Return exact live native Codex TUI process identities for a thread."""
    if not _THREAD_ID_RE.fullmatch(session_id or ""):
        return []
    try:
        process_ids = [int(item) for item in os.listdir("/proc") if item.isdigit()]
    except OSError:
        return []
    found = []
    from fleet.collectors import codex as codex_collector
    codex_observation = codex_collector.DaemonIdentityObservation()
    for pid in process_ids:
        try:
            kind, service = _runtime(pid)
            record = _process(pid)
            if kind != "codex" or service or not record or _registered_worker(pid):
                continue
            if (_native_session(pid, "codex", codex_observation=codex_observation) != session_id
                    or _process(pid) != record):
                continue
            found.append({"pid": pid, "pid_start": str(record[5]), "session_id": session_id})
        except Exception:
            continue
    return found


def _same_socket_endpoint(owner_socket, requested_socket, server):
    requested = (os.environ.get("HERDR_SOCKET_PATH", "")
                 if requested_socket is None else str(requested_socket or ""))
    if server and not requested:
        return False
    if bool(owner_socket) != bool(requested):
        return False
    if not owner_socket:
        return True
    return os.path.realpath(owner_socket) == os.path.realpath(requested)


def verified_pane(pane, harness, sid=None, *, pid=None, executable=None, server=None, socket_path=None):
    """Return the hinted pane only with a live physical or exact session binding.

    Tools and hooks may have pipes for stdio; their nearest runtime must be the
    foreground process on the pane's actual controlling tty. Shared services
    instead need the requested session proven by that foreground runtime.
    Codex selection comes from process_identity for the foreground TUI. A visible
    footer is a confirmation and rejects a conflicting thread; CODEX_THREAD_ID
    identifies shared-daemon callers, whose pane is resolved through the TUI PID.
    PID/start and foreground tty/group are re-read before accepting either path.
    """
    if harness not in {"claude", "codex", "opencode"} or (not pane and harness != "codex"):
        return ""
    executable = executable or shutil.which("herdr")
    if not executable:
        return ""
    if harness == "codex":
        try:
            caller_pid = os.getpid() if pid is None else int(pid)
            if _registered_worker(caller_pid):
                return ""
            own, service = _caller_runtime(caller_pid, "codex")
            if not own:
                return ""
            caller_sid = os.environ.get("CODEX_THREAD_ID", "")
            if sid is not None and (not isinstance(sid, str) or not _THREAD_ID_RE.fullmatch(sid)):
                return ""
            target_sid = sid if sid is not None else caller_sid
            if not target_sid and not service:
                target_sid = _native_session(own[0], "codex")
            if not target_sid or not _THREAD_ID_RE.fullmatch(target_sid):
                return ""
            pair = codex_tui_pane_endpoint(
                target_sid, pane_hint=str(pane or "").strip() or None,
                executable=executable)
            if not pair or not _same_socket_endpoint(pair[1], socket_path, server):
                return ""
            return pair[0]
        except Exception:
            return ""
    try:
        info = _pane_process_info(pane, executable=executable,
                                  server=server, socket_path=socket_path)
        if not info:
            return ""
        group = int(info.get("foreground_process_group_id") or 0)
        own, service = _caller_runtime(os.getpid() if pid is None else pid, harness)
        if not own:
            return ""
        for item in info.get("foreground_processes") or []:
            foreground = _process(item.get("pid")) if isinstance(item, dict) else None
            if not foreground or not foreground[3] or foreground[2] != group or foreground[4] != group:
                continue
            kind, foreground_service = _runtime(foreground[0])
            if kind != harness or foreground_service:
                continue
            if harness == "codex" and pid is not None and _registered_worker(foreground[0]):
                continue
            # A current footer proves selection only for the caller's physical
            # frontend (or the existing shared-service path). An inherited pane
            # hint cannot transfer ownership to another foreground Codex process.
            if (harness == "codex" and sid and not service
                    and own[0] != foreground[0]):
                continue
            native_sid = _native_session(foreground[0], harness) if sid else ""
            physical = (not service and own[0] == foreground[0]
                        and (pid is None or not sid or native_sid == sid))
            exact = bool(sid and native_sid == sid
                         and ((service and harness in {"codex", "opencode"})
                              or _native_session(own[0], harness) == sid))
            attached_bg = False
            if (harness == "claude" and sid and service
                    and (pid is None or int(pid) == os.getpid())):
                # A promoted bg-spare has no foreground tty. Its exact live
                # job declaration and this pane's attach argv form one proof.
                from session_tidy_native import caller_target, live
                target = caller_target("claude")
                record = live(target) if target else None
                if (target and record and target["pid"] == own[0]
                        and str(target["start"]) == str(own[5])
                        and record.get("sessionId") == sid
                        and record.get("jobId") == target["job"]):
                    words = [part for part in Path(f"/proc/{foreground[0]}/cmdline")
                             .read_bytes().split(b"\0") if part]
                    job = target["job"].encode()
                    attached_bg = (len(words) == 3 and _native_claude_executable(os.fsdecode(words[0]))
                                   and words[1:] == [b"attach", job])
                    if attached_bg:
                        latest = _pane_process_info(pane, executable=executable,
                                                    server=server, socket_path=socket_path)
                        latest_pids = {int(item.get("pid")) for item in (latest or {}).get("foreground_processes") or []
                                       if isinstance(item, dict) and str(item.get("pid", "")).isdigit()}
                        attached_bg = bool(int((latest or {}).get("foreground_process_group_id") or 0) == group
                                           and foreground[0] in latest_pids
                                           and live(target) == record)
            codex_current = bool(harness == "codex" and sid
                                 and (pid is None or own[0] == int(pid)))
            footer_exact = bool(
                sid and codex_current
                and _footer_proves_current_thread(
                    pane, sid, foreground[0], foreground, group, executable=executable,
                    server=server, socket_path=socket_path))
            binding = (footer_exact if codex_current else physical or exact or attached_bg)
            if (binding
                    and _process(foreground[0]) == foreground and _process(own[0]) == own):
                return str(pane)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
        pass
    return ""
