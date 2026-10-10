"""Common, host-local GPU admission for compute-hosts and resource-runner.

The dependency-free implementation can also travel over compute-hosts' existing
SSH boundary. State paths are supplied by the canonical dispatch resolver, not
by an adapter. Only the host running the GPU reads its process identities.
"""
import contextlib
import datetime
import fcntl
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import uuid


class GPUUnavailable(ValueError):
    pass


def state_path(environ=None):
    from dispatch_contract import resolve_agent_home, resolve_dispatch_state_root
    env = os.environ if environ is None else environ
    jobs = env.get("AGENT_DISPATCH_JOBS")
    return (Path(jobs).expanduser().resolve().parent if jobs else
            resolve_dispatch_state_root(resolve_agent_home(), environ=env)) / "gpu-leases.json"


def launcher_owner():
    from session_identity import identity as session_identity
    found = session_identity()
    if not found.session_id:
        return None
    owner = {"kind": "session", "harness": found.harness, "id": found.session_id}
    tools = str(Path(__file__).resolve().parents[1] / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    from fleet.session_handle import resolve_tag
    tag = resolve_tag(found.harness, found.session_id)
    owner["label"] = "%s [%s]" % (found.harness, tag) if tag else "%s:%s" % (found.harness, found.session_id[:8])
    return owner


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
    if requested == "":
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


def launch_compute(options, source):
    """Runs on the target host; reserve before detached launch and bind before CUDA."""
    path = options["state_path"]
    lease = acquire(path, options["observation"], requested=options.get("requested"),
                    share=options.get("share", False), owner=options.get("owner"),
                    task=options["task"], run_id=options["run_id"])
    devices = ",".join(lease["gpus"]) if lease else options.get("requested")
    inner = options["inner"].replace("__HEARTING_GPU_SETUP__",
        "export CUDA_VISIBLE_DEVICES=%s" % shlex.quote(devices) if devices is not None else ":")
    script = source + "\nraise SystemExit(payload(%r, %r, %r))\n" % (path, lease, inner)
    command = "python3 -c " + shlex.quote(script)
    run_dir = Path(options["run_dir"])
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
        if subprocess.call(["sh", "-c", "command -v tmux >/dev/null 2>&1"]) == 0:
            subprocess.check_call(["tmux", "new-session", "-d", "-s", options["run_id"], command])
        else:
            with open(os.devnull, "rb") as stdin, open(os.devnull, "ab") as output:
                subprocess.Popen(["python3", "-c", script], stdin=stdin, stdout=output,
                                 stderr=output, start_new_session=True)
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
    return {"gpus": devices, "gpu_lease": lease}


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
    if requested == "" or (requested is None and node.get("resource_class") != "gpu" and not gpu_scoped):
        return None, None, {}
    observation = local_observation()
    path = state_path({**os.environ, **({"AGENT_DISPATCH_JOBS": str(jobs)} if jobs else {})})
    lease = acquire(path, observation, requested=requested, share=share, owner=launcher_owner(),
                    task=run_id or shlex.join(command), run_id=run_id)
    if not lease:
        return path, None, {}
    source = Path(__file__).read_text()
    cleanup = source + "\nrelease(%r, %r)\n" % (str(path), lease)
    return path, lease, {"CUDA_VISIBLE_DEVICES": ",".join(lease["gpus"]),
                         "HEARTING_GPU_LEASE_RELEASE": "python3 -c " + shlex.quote(cleanup)}
