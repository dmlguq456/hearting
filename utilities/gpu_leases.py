"""Common, host-local GPU admission for compute-hosts and resource-runner.

The dependency-free implementation can also travel over compute-hosts' existing
SSH boundary. State paths are supplied by the canonical dispatch resolver, not
by an adapter. Only the host running the GPU reads its process identities.
"""
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import uuid


class GPUUnavailable(ValueError):
    pass


_TMUX_SESSION_ENV_KEYS = (
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID",
    "CODEX_THREAD_ID", "CODEX_SESSION_ID", "OPENCODE_SESSION_ID",
)
_TMUX_SERVER_PIN_KEYS = {
    "AGENT_HOME", "CLAUDE_HOME", "AGENT_DISPATCH_ATTEMPT_ID",
    "AGENT_DISPATCH_DEPTH", "AGENT_DISPATCH_JOBS",
    "AGENT_OWNER_ROUTE_FILE", "AGENT_ROUTE_FILE",
    "AGENT_DISPATCH_CALLER_HARNESS", "AGENT_DISPATCH_CURRENT_HARNESS",
    "TMUX", "TMUX_PANE",
    "HEARTING_COMPUTE_RUN_ID", "HEARTING_COMPUTE_HOST",
    "HEARTING_RESOURCE_RUN_ID", "HEARTING_GPU_LEASE_RELEASE",
} | set(_TMUX_SESSION_ENV_KEYS)

_TMUX_LAUNCH_DEFAULT_KEYS = (
    *_TMUX_SESSION_ENV_KEYS,
    "AGENT_HOME", "AGENT_DISPATCH_ATTEMPT_ID",
    "HEARTING_COMPUTE_RUN_ID", "HEARTING_COMPUTE_HOST",
    "HEARTING_RESOURCE_RUN_ID", "HEARTING_GPU_LEASE_RELEASE",
)


def _validated_tmux_launch_environment(environment):
    """Return a validated, default-complete per-launch environment."""
    if not isinstance(environment, dict):
        raise GPUUnavailable("invalid tmux launch environment")
    result = dict(environment)
    for key in _TMUX_LAUNCH_DEFAULT_KEYS:
        result.setdefault(key, "")
    for key, value in result.items():
        if (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                or not isinstance(value, str) or "\0" in value):
            raise GPUUnavailable("invalid tmux launch environment")
    return result


def _tmux_launch_command(environment, command):
    """Prefix one command with the exact validated environment for its wrapper."""
    assignments = [shlex.quote("%s=%s" % (key, environment[key]))
                   for key in sorted(environment)]
    return "exec env " + " ".join(assignments) + " " + command


def _run_tmux_launch(operation, argv, environment):
    """Run a tmux client with bounded diagnostics and no argv disclosure."""
    captures = []
    try:
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            result = subprocess.run(argv, env=environment, cwd="/", stdout=stdout_file,
                                    stderr=stderr_file, check=False)
            for handle in (stdout_file, stderr_file):
                handle.seek(0)
                raw = handle.read(4097)
                clipped = len(raw) > 4096
                value = raw[:4096].decode("utf-8", "replace")
                captures.append(value + (" …[truncated]" if clipped else ""))
        if result.returncode != 0:
            raise GPUUnavailable(json.dumps({
                "operation": operation, "exit_code": result.returncode,
                "stdout": captures[0], "stderr": captures[1],
            }, ensure_ascii=False, sort_keys=True))
    except OSError as exc:
        raise GPUUnavailable(json.dumps({
            "operation": operation, "exit_code": None,
            "stdout": "", "stderr": str(exc)[:4096],
        }, ensure_ascii=False, sort_keys=True)) from exc


def tmux_server_name(state_file):
    """Stable private tmux namespace for this host-local Hearting state root."""
    state = Path(state_file).expanduser()
    if not state.is_absolute():
        raise GPUUnavailable("GPU state path must be absolute for managed tmux")
    root = state.parent.resolve(strict=False)
    digest = hashlib.sha256(os.fsencode(str(root))).hexdigest()[:20]
    return "hearting-" + digest


def state_path(environ=None):
    from dispatch_contract import resolve_agent_home, resolve_dispatch_state_root
    env = os.environ if environ is None else environ
    jobs = env.get("AGENT_DISPATCH_JOBS")
    return (Path(jobs).expanduser().resolve().parent if jobs else
            resolve_dispatch_state_root(resolve_agent_home(), environ=env)) / "gpu-leases.json"


_OWNER_UNSET = object()


def _valid_session(harness, session_id):
    return (harness in {"claude", "codex", "opencode"}
            and isinstance(session_id, str) and bool(session_id)
            and len(session_id) <= 256 and session_id == session_id.strip()
            and not any(ord(char) < 32 or ord(char) == 127 for char in session_id))


def _session_owner(harness, session_id):
    if not _valid_session(harness, session_id):
        return None
    owner = {"kind": "session", "harness": harness, "id": session_id}
    tag = None
    try:
        tools = str(Path(__file__).resolve().parents[1] / "tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        from fleet.session_handle import resolve_tag
        tag = resolve_tag(harness, session_id)
    except Exception:  # noqa: BLE001 - labels are optional; exact identity is not
        pass
    owner["label"] = ("%s [%s]" % (harness, tag) if tag
                      else "%s:%s" % (harness, session_id[:8]))
    return owner


def _registered_parent(environ, jobs=None):
    """Resolve a registered launcher to its route parent's effective session.

    The boolean distinguishes an unregistered/interactive launcher, which may use its
    own canonical identity, from a registered attempt whose lineage could not be proved.
    """
    attempt = environ.get("AGENT_DISPATCH_ATTEMPT_ID")
    if not attempt:
        return False, None
    raw_depth = environ.get("AGENT_DISPATCH_DEPTH")
    if isinstance(raw_depth, int) and not isinstance(raw_depth, bool):
        depth = raw_depth
    elif isinstance(raw_depth, str) and raw_depth in {"0", "1", "2"}:
        depth = int(raw_depth)
    else:
        # An attempt id is evidence that this may be registered. Missing or
        # malformed depth cannot turn it into an interactive launcher.
        return True, None
    if depth == 0:
        return False, None
    if depth not in (1, 2):
        return True, None

    try:
        if jobs is None:
            jobs = environ.get("AGENT_DISPATCH_JOBS")
        if not jobs:
            from dispatch_contract import stable_state_root
            jobs = stable_state_root(environ) / "jobs.log"
        jobs = Path(jobs)

        utilities = str(Path(__file__).resolve().parent)
        if utilities not in sys.path:
            sys.path.insert(0, utilities)
        import dispatch_seat_handover as handover

        rows = handover.latest_rows(jobs)
        current = rows.get(attempt)
        if not current:
            return True, None
        _status, metadata = current
        if (metadata.get("attempt_id") != attempt
                or str(metadata.get("dispatch_depth") or "") != str(depth)):
            return True, None

        owner_meta = metadata
        if depth == 2:
            parent_attempt = metadata.get("parent_attempt_id")
            parent = rows.get(parent_attempt) if parent_attempt else None
            if not parent:
                return True, None
            _parent_status, owner_meta = parent
            if (owner_meta.get("attempt_id") != parent_attempt
                    or str(owner_meta.get("dispatch_depth") or "") != "1"
                    or not handover.eligible_row(owner_meta)):
                return True, None
            child_route, child_hash, _child_node = handover.route_identity(metadata)
            parent_route, parent_hash, _parent_node = handover.route_identity(owner_meta)
            if (not child_route or not child_hash
                    or (child_route, child_hash) != (parent_route, parent_hash)):
                return True, None
        elif not handover.eligible_row(owner_meta):
            return True, None

        # _effective reads both the confirmed same-seat handover and its harness from
        # the same snapshot, preserving the registered parent when no handover exists.
        session_id, harness = handover._effective(owner_meta, jobs)
        if not _valid_session(harness, session_id):
            if not _valid_session_id(session_id):
                return True, None
            tools = str(Path(__file__).resolve().parents[1] / "tools")
            if tools not in sys.path:
                sys.path.insert(0, tools)
            from fleet import route_chain
            matches = []
            for candidate in route_chain.HARNESSES:
                try:
                    if Path(route_chain.ledger_path(candidate, session_id)).is_file():
                        matches.append(candidate)
                except (OSError, ValueError):
                    continue
            if len(matches) != 1:
                return True, None
            harness = matches[0]
        return True, _session_owner(harness, session_id)
    except Exception:  # noqa: BLE001 - attribution is advisory and must not block launch
        return True, None


def _valid_session_id(session_id):
    return (isinstance(session_id, str) and bool(session_id)
            and len(session_id) <= 256 and session_id == session_id.strip()
            and not any(ord(char) < 32 or ord(char) == 127 for char in session_id))


def launcher_owner(environ=None, jobs=None):
    """Return one canonical launcher session, resolving registered attempts to their parent."""
    env = os.environ if environ is None else environ
    registered, owner = _registered_parent(env, jobs)
    if registered:
        return owner
    try:
        from session_identity import identity as session_identity
        found = session_identity(env)
    except Exception:  # noqa: BLE001 - unknown identity leaves the run unattributed
        return None
    if not found.known or not _valid_session(found.harness, found.session_id):
        return None
    return _session_owner(found.harness, found.session_id)


def launcher_session_environment(environ=None, *, owner=_OWNER_UNSET):
    """Copy an environment with every harness session alias replaced by one exact owner."""
    values = dict(os.environ if environ is None else environ)
    from session_identity import session_env
    declared = session_env()
    keys = tuple(key for names in declared.values() for key in names)
    for key in keys:
        values.pop(key, None)
    if owner is _OWNER_UNSET:
        owner = launcher_owner(environ)
    if (not isinstance(owner, dict) or owner.get("kind") != "session"
            or not _valid_session(owner.get("harness"), owner.get("id"))):
        return values
    names = declared.get(owner["harness"]) or ()
    if names:
        values[names[0]] = owner["id"]
    return values


def session_environment_keys():
    from session_identity import session_env
    return tuple(key for names in session_env().values() for key in names)


_REMOTE_LEASE_CORE = '''import contextlib, fcntl, json, os, socket, tempfile
from pathlib import Path

class GPUUnavailable(ValueError):
    pass

def identity(pid):
    try:
        fields = Path("/proc/%s/stat" % pid).read_text().rsplit(") ", 1)[1].split()
        return {"pid": int(pid), "starttime": fields[19],
                "pid_namespace": os.readlink("/proc/self/ns/pid"), "state": fields[0]}
    except FileNotFoundError:
        return None

def living(record):
    if record.get("host") != socket.gethostname().lower().split(".")[0]:
        return None
    if record.get("pid_namespace") != os.readlink("/proc/self/ns/pid"):
        return None
    try:
        current = identity(record["pid"])
    except (OSError, ValueError, IndexError, KeyError):
        return None
    return bool(current and current["state"] not in ("Z", "X")
                and current["starttime"] == str(record.get("starttime")))

def _read(path):
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {"schema_version": 1, "leases": {}}
    if data.get("schema_version") != 1 or not isinstance(data.get("leases"), dict):
        raise ValueError("invalid GPU reservation state")
    return data

def _write(path, data):
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(data, output, ensure_ascii=False, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, str(path))
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)

@contextlib.contextmanager
def locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _read(path)
        data["leases"] = {key: row for key, row in data["leases"].items()
                          if living(row) is not False}
        try:
            yield data
        finally:
            _write(path, data)

def release(path, lease):
    if not lease:
        return
    with locked(path) as data:
        data["leases"].pop(lease["token"], None)
'''


def serialized_snapshot_source():
    return _REMOTE_LEASE_CORE + '''
def snapshot(path):
    data = _read(path)
    host = socket.gethostname().lower().split(".")[0]
    return [row for row in data["leases"].values()
            if row.get("host") == host and living(row) is not False]
'''


def serialized_payload_source():
    return _REMOTE_LEASE_CORE + '''
import subprocess

def bind(path, lease, process):
    if not lease:
        return
    with locked(path) as data:
        row = data["leases"].get(lease["token"])
        if row is None:
            raise GPUUnavailable("GPU reservation ended before payload launch")
        row.update(process)

def payload(path, lease, inner):
    bind(path, lease, identity(os.getpid()))
    try:
        return subprocess.call(["bash", "-lc", inner])
    finally:
        release(path, lease)
'''


def identity(pid):
    try:
        text = Path("/proc/%s/stat" % pid).read_text()
        fields = text.rsplit(") ", 1)[1].split()
        return {"pid": int(pid), "starttime": fields[19],
                "pid_namespace": os.readlink("/proc/self/ns/pid"), "state": fields[0]}
    except FileNotFoundError:
        return None


def living(record):
    """False is proven death; unreadable/foreign namespace stays occupied."""
    if record.get("host") != socket.gethostname().lower().split(".")[0]:
        return None
    if record.get("pid_namespace") != os.readlink("/proc/self/ns/pid"):
        return None
    try:
        current = identity(record["pid"])
    except (OSError, ValueError, IndexError, KeyError):
        return None
    return bool(current and current["state"] not in ("Z", "X")
                and current["starttime"] == str(record.get("starttime")))


def _read(path):
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {"schema_version": 1, "leases": {}}
    if data.get("schema_version") != 1 or not isinstance(data.get("leases"), dict):
        raise ValueError("invalid GPU reservation state")
    return data


def _write(path, data):
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(data, output, ensure_ascii=False, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, str(path))
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


@contextlib.contextmanager
def locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _read(path)
        data["leases"] = {key: row for key, row in data["leases"].items()
                          if living(row) is not False}
        try:
            yield data
        finally:
            _write(path, data)


def snapshot(path):
    # Probes are read-only, including on a remote host. Atomic writers already
    # publish complete JSON; prune the view here and persist cleanup at admission.
    data = _read(path)
    host = socket.gethostname().lower().split(".")[0]
    return [row for row in data["leases"].values()
            if row.get("host") == host and living(row) is not False]


def owner_label(owner):
    owner = owner or {}
    return owner.get("label") or ("%s:%s" % (owner.get("harness", "?"),
                                           str(owner.get("id", "unknown"))[:8]))


def description(row):
    stamp = row.get("started_at")
    when = datetime.datetime.fromtimestamp(stamp).isoformat(timespec="seconds") \
        if isinstance(stamp, (int, float)) else "unknown"
    return "%s · %s · %s" % (owner_label(row.get("owner")), row.get("task", "?"), when)


def select(observation, leases, requested=None, share=False):
    if requested in {"", "-1"}:
        return []  # explicit CPU-only
    if (not observation.get("reachable") or observation.get("detail")
            or observation.get("reservation_detail") or observation.get("gpu_status")
            or any(g.get("observation_source") for g in observation.get("gpus", []))):
        status = observation.get("gpu_status") or {}
        if status.get("summary"):
            occupied = sorted({str(g["index"]) for g in observation.get("gpus", [])
                               if g.get("processes")}, key=int)
            suffix = " · 장치 점유 " + ", ".join("GPU " + d for d in occupied) if occupied else ""
            raise GPUUnavailable(status["summary"] + suffix + " · 새 GPU 실행 예약 불가")
        raise GPUUnavailable("GPU availability unknown: %s" % (observation.get("detail") or "probe unavailable"))
    gpus = {str(g["index"]): g for g in observation.get("gpus", [])}
    if not gpus and requested is None:
        return []  # genuinely GPU-less host
    busy = {}
    for row in leases:
        for device in row["gpus"]:
            busy.setdefault(device, []).append(description(row))
    for device, gpu in gpus.items():
        for process in gpu.get("processes") or []:
            owner = process.get("session_owner") or process.get("owner")
            busy.setdefault(device, []).append("%s · %s · pid %s" % (
                owner_label(owner), process.get("command") or process.get("process_name") or "GPU process",
                process.get("pid", "?")))
        if gpu.get("utilization_gpu_pct") != 0 and device not in busy:
            busy[device] = ["GPU active or utilization unknown"]
        if observation.get("process_detail") and device not in busy:
            busy[device] = ["GPU processes unknown"]
    free = sorted((device for device in gpus if device not in busy),
                  key=lambda d: (-(gpus[d].get("free_mib") or 0), int(d)))
    if requested is None:
        devices = free[:1]
        if share and not devices:
            devices = sorted(gpus, key=lambda d: (-(gpus[d].get("free_mib") or 0), int(d)))[:1]
    else:
        devices = []
        for value in str(requested).split(","):
            value = value.strip()
            matches = [d for d, gpu in gpus.items() if value in (d, gpu.get("uuid"))]
            if len(matches) != 1:
                raise GPUUnavailable("unknown GPU %s; available indexes: %s" % (value, ",".join(gpus) or "none"))
            if matches[0] not in devices:
                devices.append(matches[0])
    conflicts = [d for d in devices if d in busy]
    if not devices or (conflicts and not share):
        details = "; ".join("gpu%s: %s" % (d, " / ".join(busy[d]))
                            for d in (conflicts or sorted(busy)))
        raise GPUUnavailable("GPU in use — %s; free GPUs: %s%s" % (
            details, ", ".join("gpu" + d for d in free) or "none",
            "; --share allows intentional sharing"))
    return devices


def acquire(path, observation, *, requested=None, share=False, owner=None, task="", run_id=""):
    with locked(path) as data:
        host = socket.gethostname().lower().split(".")[0]
        leases = [row for row in data["leases"].values() if row.get("host") == host]
        devices = select(observation, leases, requested, share)
        if not devices:
            return None
        token = uuid.uuid4().hex
        row = {"token": token, "host": socket.gethostname().lower().split(".")[0],
               "gpus": devices, "owner": owner, "task": task, "run_id": run_id,
               "started_at": time.time(), "share": bool(share), **identity(os.getpid())}
        data["leases"][token] = row
        return row


def bind(path, lease, process):
    if not lease:
        return
    with locked(path) as data:
        row = data["leases"].get(lease["token"])
        if row is None:
            raise GPUUnavailable("GPU reservation ended before payload launch")
        row.update(process)


def release(path, lease):
    if not lease:
        return
    with locked(path) as data:
        data["leases"].pop(lease["token"], None)


def payload(path, lease, inner):
    bind(path, lease, identity(os.getpid()))
    try:
        return subprocess.call(["bash", "-lc", inner])
    finally:
        release(path, lease)


def launch_compute(options, payload_source):
    """Runs on the target host; reserve before detached launch and bind before CUDA."""
    path = options["state_path"]
    lease = acquire(path, options["observation"], requested=options.get("requested"),
                    share=options.get("share", False), owner=options.get("owner"),
                    task=options["task"], run_id=options["run_id"])
    devices = ",".join(lease["gpus"]) if lease else options.get("requested")
    inner = options["inner"].replace("__HEARTING_GPU_SETUP__",
        "export CUDA_VISIBLE_DEVICES=%s" % shlex.quote(devices) if devices is not None else ":")
    script = payload_source + "\nraise SystemExit(payload(%r, %r, %r))\n" % (path, lease, inner)
    command = "python3 -c " + shlex.quote(script)
    run_dir = Path(options["run_dir"])
    launch_cwd = options.get("cwd") or os.getcwd()
    if not isinstance(launch_cwd, str) or not os.path.isabs(launch_cwd):
        release(path, lease)
        raise GPUUnavailable("compute launch directory must be absolute")
    try:
        tmux_environment = _validated_tmux_launch_environment(
            options.get("tmux_environment") or {})
    except GPUUnavailable:
        release(path, lease)
        raise
    server_env = dict(os.environ)
    for key in _TMUX_SERVER_PIN_KEYS | set(tmux_environment):
        server_env.pop(key, None)
    server_name = tmux_server_name(path)
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
        if subprocess.call(
            ["sh", "-c", "command -v tmux >/dev/null 2>&1"],
            env=server_env,
            cwd="/",
        ) == 0:
            # The first client owns server creation. Keep that long-lived
            # process outside the invoking release/session, then provide only
            # this launch's validated identity and cwd to its new session.
            _run_tmux_launch("start-server",
                             ["tmux", "-L", server_name, "start-server"], server_env)
            tmux_command = ["tmux", "-L", server_name, "new-session", "-d",
                            "-s", options["run_id"]]
            tmux_command.extend(["-c", launch_cwd,
                                 _tmux_launch_command(tmux_environment, command)])
            _run_tmux_launch("new-session", tmux_command, server_env)
            tmux_transport = {"kind": "hearting-tmux-v1", "server": server_name}
        else:
            with open(os.devnull, "rb") as stdin, open(os.devnull, "ab") as output:
                subprocess.Popen(["python3", "-c", script], stdin=stdin, stdout=output,
                                 stderr=output, start_new_session=True, cwd=launch_cwd)
            tmux_transport = {"kind": "nohup-v1"}
        # The launcher remains the pending holder until its wrapper is bound.
        # A dead launcher can never let an unbound wrapper start a payload.
        deadline = time.monotonic() + 5
        while lease and time.monotonic() < deadline:
            with locked(path) as data:
                current = data["leases"].get(lease["token"])
                if current is None or current["pid"] != lease["pid"]:
                    break
            time.sleep(.02)
        else:
            if lease:
                raise GPUUnavailable("GPU launch did not establish its wrapper")
    except Exception:
        release(path, lease)
        raise
    return {"gpus": devices, "gpu_lease": lease,
            "tmux_transport": tmux_transport}


def requested_devices(command, default=None):
    requested = default
    for word in command:
        if word.startswith("CUDA_VISIBLE_DEVICES="):
            requested = word.split("=", 1)[1]
    return requested


def local_observation():
    import importlib.util
    spec = importlib.util.spec_from_file_location("_lease_compute_hosts", Path(__file__).with_name("compute-hosts.py"))
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    return tool._run_gpu_observation(socket.gethostname(), {"ssh_host": "local"})


def resource_admission(node, command, *, gpu_scoped=False, share=False, jobs=None, run_id=""):
    """Use the route's GPU declaration and the existing CUDA environment choice."""
    requested = requested_devices(command, os.environ.get("CUDA_VISIBLE_DEVICES"))
    if requested in {"", "-1"} or (requested is None and node.get("resource_class") != "gpu" and not gpu_scoped):
        return None, None, {}
    observation = local_observation()
    path = state_path({**os.environ, **({"AGENT_DISPATCH_JOBS": str(jobs)} if jobs else {})})
    try:
        lease = acquire(path, observation, requested=requested, share=share,
                        owner=launcher_owner(jobs=jobs),
                        task=run_id or shlex.join(command), run_id=run_id)
    except GPUUnavailable as error:
        raise GPUUnavailable("Local GPU admission on %s: %s" % (socket.gethostname(), error)) from error
    if not lease:
        return path, None, {}
    cleanup = _REMOTE_LEASE_CORE + "\nrelease(%r, %r)\n" % (str(path), lease)
    return path, lease, {"CUDA_VISIBLE_DEVICES": ",".join(lease["gpus"]),
                         "HEARTING_GPU_LEASE_RELEASE": "python3 -c " + shlex.quote(cleanup)}
# BEGIN PATH_NAMESPACE EXTENSION
def serialized_compute_source(namespaced=False):
    """Return the unchanged legacy prefix, or the complete remote module."""
    source = Path(__file__).read_bytes()
    marker = b"# BEGIN PATH_NAMESPACE EXTENSION\n"
    prefix, found, _suffix = source.partition(marker)
    if not found:
        raise GPUUnavailable("path namespace extension delimiter is missing")
    return source.decode("utf-8") if namespaced else prefix.decode("utf-8")


_PATH_NAMESPACE_SOURCE = r'''import ctypes as _ns_ctypes
import errno as _ns_errno
import json as _ns_json
import os as _ns_os
import re as _ns_re
import stat as _ns_stat

class _NamespaceRefusal(Exception):
    def __init__(self, code, message):
        super().__init__("path_namespace refused: " + message)
        self.code = code

def _ns_canonical(path):
    return (isinstance(path, str) and path.startswith("/") and path != "/"
            and "//" not in path and not path.endswith("/")
            and not any(ord(c) < 32 or ord(c) == 127 for c in path)
            and all(p not in ("", ".", "..") for p in path[1:].split("/")))

def _ns_below(parent, child):
    return child.startswith(parent.rstrip("/") + "/")

def _ns_validate(mapping, run_root):
    if not isinstance(mapping, dict) or not mapping:
        raise _NamespaceRefusal("unsafe-path", "unsafe source/target path: mapping")
    pairs = []
    for target, source in mapping.items():
        if not _ns_canonical(target) or not _ns_canonical(source):
            raise _NamespaceRefusal("unsafe-path", "unsafe source/target path: " + repr(target))
        if target == source:
            raise _NamespaceRefusal("unsafe-path", "unsafe source/target path: " + target)
        pairs.append((target, source))
    for i, (target, source) in enumerate(pairs):
        for other_target, other_source in pairs[i + 1:]:
            if (_ns_below(target, other_target) or _ns_below(other_target, target)):
                raise _NamespaceRefusal("unsafe-path", "unsafe source/target path: " + other_target)
        if any(source == t or _ns_below(source, t) or _ns_below(t, source)
               for t, _ in pairs):
            raise _NamespaceRefusal("unsafe-path", "unsafe source/target path: " + source)
    covers = [(t, s) for t, s in pairs if _ns_below(t, run_root)]
    if len(covers) != 1:
        raise _NamespaceRefusal("unsafe-path", "unsafe source/target path: " + run_root)
    return pairs, covers[0]

def _ns_mountinfo():
    rows = []
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as handle:
            for line in handle:
                fields = line.rstrip("\n").split(" ")
                if len(fields) < 10 or "-" not in fields:
                    continue
                sep = fields.index("-")
                unescape = lambda x: _ns_re.sub(r"\\([0-7]{3})",
                    lambda m: chr(int(m.group(1), 8)), x)
                rows.append((int(fields[0]), unescape(fields[4]),
                             unescape(fields[sep + 2])))
    except OSError:
        return []
    return rows

def _ns_errno_message(operation, err):
    name = _ns_errno.errorcode.get(err, "ERRNO")
    return operation + " failed: %s (%d)" % (name, err)

def _ns_fail_errno(code, operation, err, extra=""):
    message = _ns_errno_message(operation, err)
    if extra:
        message = extra + ": " + message
    raise _NamespaceRefusal(code, message)

def _ns_mount_id(path, rows):
    candidates = [(index, mid, mount) for index, (mid, mount, _source) in enumerate(rows)
                  if path == mount or _ns_below(mount, path)]
    # Mountinfo lists stacked mounts in order; the last longest match is visible.
    return max(candidates, key=lambda row: (len(row[2]), row[0]))[1] if candidates else None

def _ns_enter(mapping, run_root, cwd=None, run_dir=None):
    pairs, covered = _ns_validate(mapping, run_root)
    uid, gid = _ns_os.getuid(), _ns_os.getgid()
    if not uid or not gid or _ns_os.geteuid() != uid or _ns_os.getegid() != gid:
        raise _NamespaceRefusal("unsafe-path", "unsafe source/target path: effective identity")
    captured = []
    rows = _ns_mountinfo()
    for target, source in pairs:
        if not _ns_os.path.exists(source):
            raise _NamespaceRefusal("source-missing", "source missing: " + source)
        if not _ns_os.path.isdir(source) or _ns_os.path.realpath(source) != source:
            raise _NamespaceRefusal("unsafe-path", "unsafe source/target path: " + source)
        if not _ns_os.path.ismount(source):
            raise _NamespaceRefusal("source-not-mount", "source is not a mount point: " + source)
        source_stat = _ns_os.stat(source, follow_symlinks=False)
        mount_id = _ns_mount_id(source, rows)
        if mount_id is None:
            raise _NamespaceRefusal("source-not-mount", "source is not a mount point: " + source)
        if (_ns_os.path.islink(target) or _ns_os.path.realpath(target) != target
                or not _ns_os.path.isdir(target)):
            raise _NamespaceRefusal("unsafe-path", "unsafe source/target path: " + target)
        captured.append((target, source, source_stat.st_dev, source_stat.st_ino, mount_id))
    target, source = covered
    physical_root = source + run_root[len(target):]
    try:
        root_stat = _ns_os.stat(physical_root, follow_symlinks=False)
        if (_ns_os.path.realpath(physical_root) != physical_root
                or not _ns_stat.S_ISDIR(root_stat.st_mode)
                or _ns_mount_id(physical_root, rows) != _ns_mount_id(source, rows)):
            raise OSError
        root_fd = _ns_os.open(physical_root, _ns_os.O_RDONLY | _ns_os.O_DIRECTORY | _ns_os.O_NOFOLLOW | _ns_os.O_CLOEXEC)
    except OSError:
        raise _NamespaceRefusal("run-root-missing", "run_root is not an existing source directory: " + physical_root)
    run_fd = None
    if run_dir:
        physical_run = source + run_dir[len(target):]
        try:
            run_stat = _ns_os.stat(physical_run, follow_symlinks=False)
            if (_ns_os.path.realpath(physical_run) != physical_run
                    or not _ns_stat.S_ISDIR(run_stat.st_mode)
                    or _ns_mount_id(physical_run, rows) != _ns_mount_id(source, rows)):
                raise OSError
            run_fd = _ns_os.open(physical_run, _ns_os.O_RDONLY | _ns_os.O_DIRECTORY | _ns_os.O_NOFOLLOW | _ns_os.O_CLOEXEC)
        except OSError:
            _ns_os.close(root_fd)
            raise _NamespaceRefusal("unsafe-path", "unsafe source/target path: " + physical_run)
    libc = _ns_ctypes.CDLL(None, use_errno=True)
    libc.unshare.argtypes = [_ns_ctypes.c_int]
    libc.unshare.restype = _ns_ctypes.c_int
    if libc.unshare(0x10000000 | 0x00020000) != 0:
        err = _ns_ctypes.get_errno()
        _ns_os.close(root_fd)
        if run_fd is not None: _ns_os.close(run_fd)
        _ns_fail_errno("unshare", "unshare", err)
    for path, content, code in (("/proc/self/setgroups", "deny\n", "setgroups"),
                                ("/proc/self/uid_map", "%d %d 1\n" % (uid, uid), "uid-map"),
                                ("/proc/self/gid_map", "%d %d 1\n" % (gid, gid), "gid-map")):
        try:
            with open(path, "w", encoding="ascii") as handle: handle.write(content)
        except OSError as exc:
            _ns_os.close(root_fd)
            if run_fd is not None: _ns_os.close(run_fd)
            _ns_fail_errno(code, code, exc.errno or 1)
    libc.mount.argtypes = [_ns_ctypes.c_char_p, _ns_ctypes.c_char_p,
                           _ns_ctypes.c_char_p, _ns_ctypes.c_ulong, _ns_ctypes.c_void_p]
    libc.mount.restype = _ns_ctypes.c_int
    if libc.mount(None, b"/", None, 16384 | 262144, None) != 0:
        err = _ns_ctypes.get_errno()
        _ns_os.close(root_fd)
        if run_fd is not None: _ns_os.close(run_fd)
        _ns_fail_errno("private-mount", "private mount", err)
    for target, source, _dev, _ino, _mid in captured:
        if libc.mount(source.encode(), target.encode(), None, 4096 | 16384, None) != 0:
            err = _ns_ctypes.get_errno()
            _ns_os.close(root_fd)
            if run_fd is not None: _ns_os.close(run_fd)
            errno_name = _ns_errno.errorcode.get(err, "ERRNO")
            raise _NamespaceRefusal("bind-mount", "rbind failed: %s -> %s: %s (%d)" %
                                    (source, target, errno_name, err))
    visible_root = _ns_os.stat(run_root)
    if not _ns_stat.S_ISDIR(visible_root.st_mode) or (visible_root.st_dev, visible_root.st_ino) != (root_stat.st_dev, root_stat.st_ino):
        _ns_os.close(root_fd)
        if run_fd is not None: _ns_os.close(run_fd)
        raise _NamespaceRefusal("identity-mismatch", "run_root identity mismatch: " + run_root)
    for target, source, dev, ino, _mount_id in captured:
        visible = _ns_os.stat(target)
        if (visible.st_dev, visible.st_ino) != (dev, ino):
            _ns_os.close(root_fd)
            if run_fd is not None: _ns_os.close(run_fd)
            raise _NamespaceRefusal("identity-mismatch", "bound target identity mismatch: " + target)
    if run_dir:
        visible_run = _ns_os.stat(run_dir)
        if (visible_run.st_dev, visible_run.st_ino) != (run_stat.st_dev, run_stat.st_ino):
            _ns_os.close(root_fd)
            if run_fd is not None: _ns_os.close(run_fd)
            raise _NamespaceRefusal("identity-mismatch", "run_root identity mismatch: " + run_root)
    desired = cwd or "/"
    if not isinstance(desired, str) or not desired.startswith("/"):
        _ns_os.close(root_fd)
        if run_fd is not None: _ns_os.close(run_fd)
        raise _NamespaceRefusal("cwd", "chdir failed: " + str(desired) + ": EINVAL (22)")
    try: _ns_os.chdir(desired)
    except OSError as exc:
        _ns_os.close(root_fd)
        if run_fd is not None: _ns_os.close(run_fd)
        _ns_fail_errno("cwd", "chdir", exc.errno or 1, desired)
    return root_fd, run_fd

def _ns_safe_run_fd(mapping, run_root, run_dir):
    pairs, _covered = _ns_validate(mapping, run_root)
    target, source = next((t, s) for t, s in pairs if _ns_below(t, run_dir))
    rows = _ns_mountinfo()
    if not _ns_os.path.exists(source):
        raise _NamespaceRefusal("source-missing", "source missing: " + source)
    if (not _ns_os.path.isdir(source) or _ns_os.path.realpath(source) != source):
        raise _NamespaceRefusal("unsafe-path", "unsafe source/target path: " + source)
    if not _ns_os.path.ismount(source):
        raise _NamespaceRefusal("source-not-mount", "source is not a mount point: " + source)
    physical = source + run_dir[len(target):]
    source_mount_id = _ns_mount_id(source, rows)
    if (source_mount_id is None or _ns_os.path.realpath(physical) != physical
            or _ns_mount_id(physical, rows) != source_mount_id):
        raise _NamespaceRefusal("unsafe-path", "unsafe source/target path: " + physical)
    try:
        return _ns_os.open(physical, _ns_os.O_RDONLY | _ns_os.O_DIRECTORY
                           | _ns_os.O_NOFOLLOW | _ns_os.O_CLOEXEC)
    except OSError:
        raise _NamespaceRefusal("unsafe-path", "unsafe source/target path: " + physical)

def _ns_record_refusal(run_fd, error):
    reason_fd = _ns_os.open("stop_reason", _ns_os.O_WRONLY | _ns_os.O_CREAT
                            | _ns_os.O_EXCL | _ns_os.O_NOFOLLOW, 0o600,
                            dir_fd=run_fd)
    try:
        _ns_os.write(reason_fd, ("path-namespace-" + error.code + "\n").encode())
    finally:
        _ns_os.close(reason_fd)
    exit_fd = _ns_os.open("exit_code", _ns_os.O_WRONLY | _ns_os.O_CREAT
                          | _ns_os.O_EXCL | _ns_os.O_NOFOLLOW, 0o600,
                          dir_fd=run_fd)
    try:
        _ns_os.write(exit_fd, b"125\n")
    finally:
        _ns_os.close(exit_fd)

def _ns_namespace_creation(mapping, run_root, run_dir, cwd):
    read_fd, write_fd = _ns_os.pipe()
    try:
        pid = _ns_os.fork()
    except OSError as exc:
        _ns_os.close(read_fd); _ns_os.close(write_fd)
        raise _NamespaceRefusal("child", "creation child failed: " + str(exc))
    if pid == 0:
        _ns_os.close(read_fd)
        try:
            root_fd, _ = _ns_enter(mapping, run_root, cwd)
            try: _ns_os.mkdir(run_dir)
            finally: _ns_os.close(root_fd)
            receipt = {"ok": True}
        except Exception as exc:
            receipt = {"ok": False, "error": str(exc),
                       "code": getattr(exc, "code", "child")}
        try: _ns_os.write(write_fd, _ns_json.dumps(receipt).encode()[:2048])
        finally:
            _ns_os.close(write_fd); _ns_os._exit(0 if receipt.get("ok") else 1)
    _ns_os.close(write_fd)
    chunks = []
    while True:
        part = _ns_os.read(read_fd, 2048)
        if not part: break
        chunks.append(part)
    _ns_os.close(read_fd)
    _pid, status = _ns_os.waitpid(pid, 0)
    try: receipt = _ns_json.loads(b"".join(chunks))
    except Exception: receipt = {}
    if not _ns_os.WIFEXITED(status):
        raise _NamespaceRefusal("child", "creation child failed: abnormal exit")
    child_status = _ns_os.WEXITSTATUS(status)
    if child_status == 0 and receipt.get("ok") is True:
        return
    if (child_status == 1 and receipt.get("ok") is False
            and isinstance(receipt.get("code"), str)
            and isinstance(receipt.get("error"), str)
            and receipt["error"].startswith("path_namespace refused: ")):
        prefix = "path_namespace refused: "
        raise _NamespaceRefusal(receipt["code"], receipt["error"][len(prefix):])
    reason = "abnormal exit" if child_status != 0 else "missing or invalid receipt"
    raise _NamespaceRefusal("child", "creation child failed: " + reason)

def launch_compute_namespaced(options, payload_source):
    path = options["state_path"]
    lease = acquire(path, options["observation"], requested=options.get("requested"),
                    share=options.get("share", False), owner=options.get("owner"),
                    task=options["task"], run_id=options["run_id"])
    devices = ",".join(lease["gpus"]) if lease else options.get("requested")
    inner = options["inner"].replace("__HEARTING_GPU_SETUP__",
        "export CUDA_VISIBLE_DEVICES=%s" % shlex.quote(devices) if devices is not None else ":")
    mapping, run_root = options["path_namespace"], options["run_root"]
    launch_cwd = "/" if options.get("cwd") is None else options["cwd"]
    if not isinstance(launch_cwd, str) or not os.path.isabs(launch_cwd):
        release(path, lease)
        raise GPUUnavailable("compute launch directory must be absolute")
    tmux_environment = options.get("tmux_environment", {})
    if not isinstance(tmux_environment, dict):
        release(path, lease)
        raise GPUUnavailable("invalid tmux launch environment")
    runner_created = False
    try:
        _ns_namespace_creation(mapping, run_root, options["run_dir"], options.get("cwd"))
        runner_dir = Path(path).parent / "compute-runners"
        runner_dir_resolved = os.path.realpath(str(runner_dir))
        for target in mapping:
            target_resolved = os.path.realpath(target)
            if (runner_dir_resolved == target_resolved
                    or runner_dir_resolved.startswith(target_resolved.rstrip(os.sep) + os.sep)):
                raise GPUUnavailable("compute runner directory is inside a path_namespace target")
        runner_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(runner_dir, 0o700)
        runner_path = runner_dir / (options["run_id"] + ".py")
        runner = ("import os as _runner_os; _runner_os.unlink(__file__)\n"
                  + payload_source + "\n" + _PATH_NAMESPACE_SOURCE + "\n" + (
            "\nrunner_args = %r\n"
            "safe_fd = None\nroot_fd = run_fd = None\n"
            "try:\n    safe_fd = _ns_safe_run_fd(*runner_args['safe_run'])\n"
            "    root_fd, run_fd = _ns_enter(*runner_args['enter'])\n"
            "    raise SystemExit(payload(*runner_args['payload']))\n"
            "except _NamespaceRefusal as exc:\n"
            "    if safe_fd is not None:\n"
            "        try: _ns_record_refusal(safe_fd, exc)\n"
            "        except OSError: pass\n"
            "    release(*runner_args['release'])\n"
            "    print(str(exc), file=__import__('sys').stderr)\n"
            "    raise SystemExit(125)\n"
            "except Exception as raw:\n"
            "    exc = _NamespaceRefusal('runner', 'runner failed: ' + str(raw))\n"
            "    if safe_fd is not None:\n"
            "        try: _ns_record_refusal(safe_fd, exc)\n"
            "        except OSError: pass\n"
            "    try: release(*runner_args['release'])\n    except Exception: pass\n"
            "    print(str(exc), file=__import__('sys').stderr)\n"
            "    raise SystemExit(125)\n"
            "finally:\n"
            "    if root_fd is not None: _ns_os.close(root_fd)\n"
            "    if run_fd is not None: _ns_os.close(run_fd)\n"
            "    if safe_fd is not None: _ns_os.close(safe_fd)\n") % ({
                "safe_run": (mapping, run_root, options["run_dir"]),
                "enter": (mapping, run_root, options.get("cwd"), options["run_dir"]),
                "payload": (path, lease, inner),
                "release": (path, lease),
            }))
        fd = os.open(runner_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        runner_created = True
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(runner)
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                runner_path.unlink()
            raise
        command = "python3 " + shlex.quote(str(runner_path))
        env = dict(os.environ)
        tmux_environment = _validated_tmux_launch_environment(tmux_environment)
        for key in _TMUX_SERVER_PIN_KEYS | set(tmux_environment):
            env.pop(key, None)
        server = tmux_server_name(path)
        if subprocess.call(["sh", "-c", "command -v tmux >/dev/null 2>&1"], env=env, cwd="/") == 0:
            _run_tmux_launch("start-server", ["tmux", "-L", server, "start-server"], env)
            cmd = ["tmux", "-L", server, "new-session", "-d", "-s", options["run_id"]]
            cmd.extend(["-c", "/", _tmux_launch_command(tmux_environment, command)])
            _run_tmux_launch("new-session", cmd, env)
            transport = {"kind": "hearting-tmux-v1", "server": server}
        else:
            with open(os.devnull, "rb") as stdin, open(os.devnull, "ab") as output:
                subprocess.Popen(["python3", str(runner_path)], stdin=stdin, stdout=output,
                                 stderr=output, start_new_session=True, cwd="/")
            transport = {"kind": "nohup-v1"}
        deadline = time.monotonic() + 5
        while lease and time.monotonic() < deadline:
            with locked(path) as data:
                current = data["leases"].get(lease["token"])
                if current is None or current["pid"] != lease["pid"]:
                    break
            time.sleep(.02)
        else:
            if lease:
                raise GPUUnavailable("GPU launch did not establish its wrapper")
        return {"gpus": devices, "gpu_lease": lease, "tmux_transport": transport}
    except Exception:
        if runner_created:
            with contextlib.suppress(FileNotFoundError):
                runner_path.unlink()
        release(path, lease)
        raise
'''

exec(_PATH_NAMESPACE_SOURCE, globals())
