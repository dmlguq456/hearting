#!/usr/bin/env python3
"""Harness-neutral depth-0 launch broker for registered depth-2 workers.

The broker is deterministic infrastructure, not a model worker.  Conductors
submit a versioned declarative envelope over a local Unix socket; the broker
validates the immutable route/jobs binding and constructs one allowlisted
adapter command without accepting arbitrary argv, shell text, or environment.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid


SOURCE_ROOT = Path(__file__).resolve().parents[1]
_agent_home = Path(os.environ.get("AGENT_HOME", SOURCE_ROOT)).expanduser().resolve(strict=False)
ROOT = _agent_home if (_agent_home / "core/CORE.md").is_file() else SOURCE_ROOT
sys.path.insert(0, str(SOURCE_ROOT / "utilities"))
from dispatch_contract import resolve_dispatch_state_root  # noqa: E402
SCHEMA_VERSION = 1
TERMINAL = {"done", "failed", "cancelled"}
ACTIONS = {"dry-run", "register", "start"}
HARNESSES = {"claude", "codex", "opencode"}
REQUEST_ID_RE = re.compile(r"^req-[0-9a-f]{16,64}$")
ATTEMPT_ID_RE = re.compile(r"^att-[0-9a-f]{16,64}$")
MAX_REQUEST_BYTES = 1024 * 1024
DEFAULT_STALE_SECONDS = 15.0
IMPLEMENTATION_DIGEST = "sha256:" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

REQUIRED_FIELDS = {
    "schema_version", "request_id", "attempt_id", "action", "target_harness",
    "worktree", "artifact_root", "jobs", "slug", "capability", "mode",
    "intensity", "depth", "parent", "worker_role", "owner", "model_role",
    "route_file", "route_id", "route_hash", "route_node", "registry_digest",
    "write_scope", "completion_gate", "parent_harness", "parent_transport",
    "parent_sandbox", "requested_launch_authority", "fallback_ordinal",
    "probe_source", "probe_failure_class",
    "broker_root", "broker_instance",
}
OPTIONAL_FIELDS = {"prompt_file", "parent_session_id", "parent_cwd"}


class BrokerError(ValueError):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    with temp.open("x", encoding="utf-8") as handle:
        os.chmod(temp, 0o600)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BrokerError("broker-state-invalid", f"{path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BrokerError("broker-state-invalid", str(path))
    return value


def process_start_ticks(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        rest = raw[raw.rfind(")") + 2 :].split()
        return rest[19]
    except (OSError, IndexError, ValueError):
        return ""


def process_matches(pid: int, ticks: str) -> bool:
    return pid > 0 and bool(ticks) and process_start_ticks(pid) == ticks


def paths(root: Path) -> dict[str, Path]:
    return {
        "root": root,
        "socket": root / "broker.sock",
        "meta": root / "broker.json",
        "lock": root / "broker.lock",
        "log": root / "broker.log",
        "requests": root / "requests",
    }


def default_root() -> Path:
    explicit = os.environ.get("AGENT_DISPATCH_BROKER_ROOT")
    if explicit:
        return Path(explicit).expanduser().resolve(strict=False)
    return resolve_dispatch_state_root(ROOT) / "broker"


def absolute(value: object, field: str, *, must_exist: bool = False) -> Path:
    if not isinstance(value, str) or not value:
        raise BrokerError("broker-request-invalid", f"{field} is required")
    if any(char in value for char in ("\x00", "\r", "\n", "\t")):
        raise BrokerError("broker-request-invalid", f"{field} contains a control character")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise BrokerError("broker-request-invalid", f"{field} must be absolute")
    resolved = path.resolve(strict=False)
    if must_exist and not resolved.exists():
        raise BrokerError("broker-request-invalid", f"{field} does not exist: {resolved}")
    return resolved


def broker_status(root: Path, jobs: Path | None, stale_seconds: float) -> dict:
    p = paths(root)
    if not p["meta"].is_file():
        raise BrokerError("broker-unavailable", f"metadata missing: {p['meta']}")
    meta = read_json(p["meta"])
    if meta.get("schema_version") != SCHEMA_VERSION:
        raise BrokerError("broker-version-mismatch", str(meta.get("schema_version")))
    if meta.get("implementation_digest") != IMPLEMENTATION_DIGEST:
        raise BrokerError(
            "broker-implementation-mismatch",
            f"expected={IMPLEMENTATION_DIGEST} observed={meta.get('implementation_digest')}",
        )
    if jobs is not None and Path(str(meta.get("jobs", ""))).resolve(strict=False) != jobs.resolve(strict=False):
        raise BrokerError("broker-jobs-mismatch", f"expected={jobs} observed={meta.get('jobs')}")
    pid = int(meta.get("pid", 0) or 0)
    ticks = str(meta.get("start_ticks", ""))
    if not process_matches(pid, ticks):
        raise BrokerError("broker-unavailable", f"pid identity is not live: {pid}/{ticks}")
    heartbeat = float(meta.get("heartbeat_epoch", 0.0) or 0.0)
    age = max(0.0, time.time() - heartbeat)
    if age > stale_seconds:
        raise BrokerError("broker-stale", f"heartbeat_age={age:.3f}")
    expected = os.environ.get("AGENT_DISPATCH_BROKER_INSTANCE")
    if expected and expected != meta.get("instance_id"):
        raise BrokerError("broker-instance-mismatch", f"expected={expected} observed={meta.get('instance_id')}")
    return meta


def response_error(exc: BrokerError) -> dict:
    return {"ok": False, "reason": exc.reason, "detail": exc.detail}


def recv_json(conn: socket.socket) -> dict:
    chunks: list[bytes] = []
    size = 0
    while True:
        part = conn.recv(65536)
        if not part:
            break
        chunks.append(part)
        size += len(part)
        if size > MAX_REQUEST_BYTES:
            raise BrokerError("broker-request-too-large", str(size))
        if b"\n" in part:
            break
    raw = b"".join(chunks).split(b"\n", 1)[0]
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrokerError("broker-request-invalid", str(exc)) from exc
    if not isinstance(value, dict):
        raise BrokerError("broker-request-invalid", "request must be an object")
    return value


def send_json(conn: socket.socket, payload: dict) -> None:
    conn.sendall(canonical(payload) + b"\n")


def registry_attempt(jobs: Path, attempt_id: str) -> dict | None:
    if not jobs.is_file():
        return None
    for line in jobs.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = line.split("\t")
        if len(fields) != 6 or f"attempt_id={attempt_id}" not in fields[5]:
            continue
        metadata = dict(part.split("=", 1) for part in fields[5].split(",") if "=" in part)
        return {"status": fields[1], "slug": fields[4], "metadata": metadata}
    return None


def validate_route(request: dict) -> tuple[dict, dict]:
    route_path = absolute(request["route_file"], "route_file", must_exist=True)
    try:
        route = json.loads(route_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BrokerError("broker-route-invalid", str(exc)) from exc
    for key in ("route_id", "route_hash", "registry_digest"):
        if route.get(key) != request[key]:
            raise BrokerError("broker-route-mismatch", f"{key}: expected={route.get(key)} observed={request[key]}")
    if Path(str(route.get("cwd", ""))).resolve(strict=False) != Path(request["worktree"]).resolve(strict=False):
        raise BrokerError("broker-route-mismatch", "worktree")
    if Path(str(route.get("artifact_root", ""))).resolve(strict=False) != Path(request["artifact_root"]).resolve(strict=False):
        raise BrokerError("broker-route-mismatch", "artifact_root")
    if route.get("capability") != request["capability"] or route.get("effective_intensity") != request["intensity"]:
        raise BrokerError("broker-route-mismatch", "capability/intensity")
    node = next((row for row in route.get("nodes", []) if row.get("id") == request["route_node"]), None)
    if not node:
        raise BrokerError("broker-route-mismatch", "route_node")
    if sorted(node.get("write_scope", [])) != sorted(part for part in request["write_scope"].split(";") if part):
        raise BrokerError("broker-route-mismatch", "write_scope")
    if node.get("completion_gate") != request["completion_gate"]:
        raise BrokerError("broker-route-mismatch", "completion_gate")
    ordinal = int(request["fallback_ordinal"])
    hop = next((row for row in node.get("dispatch_fallback", []) if int(row.get("ordinal", 0)) == ordinal), None)
    if not hop or hop.get("hop") not in {"same-harness-headless", "cross-harness-headless"}:
        raise BrokerError("broker-route-mismatch", "fallback_ordinal")
    contract = route.get("broker_contract_version")
    candidate = next(
        (
            row for row in hop.get("candidates", [])
            if row.get("status") == "supported"
            and row.get("parent_harness") == request["parent_harness"]
            and row.get("child_harness") == request["target_harness"]
            and row.get("launch_authority") == request["requested_launch_authority"]
            and row.get("broker_root") == request["broker_root"]
            and (contract == 2 or row.get("broker_instance") == request["broker_instance"])
        ),
        None,
    )
    if candidate is None:
        raise BrokerError("broker-route-mismatch", "selected fallback candidate")
    return route, node


def validate_request(
    request: dict,
    jobs: Path,
    broker_root: Path,
    broker_instance: str,
    *,
    allowed_broker_instances: set[str] | None = None,
) -> dict:
    keys = set(request)
    missing = REQUIRED_FIELDS - keys
    unknown = keys - REQUIRED_FIELDS - OPTIONAL_FIELDS
    if missing or unknown:
        raise BrokerError("broker-request-invalid", f"missing={sorted(missing)} unknown={sorted(unknown)}")
    if request.get("schema_version") != SCHEMA_VERSION:
        raise BrokerError("broker-request-version", str(request.get("schema_version")))
    if not REQUEST_ID_RE.fullmatch(str(request.get("request_id", ""))):
        raise BrokerError("broker-request-invalid", "request_id")
    if not ATTEMPT_ID_RE.fullmatch(str(request.get("attempt_id", ""))):
        raise BrokerError("broker-request-invalid", "attempt_id")
    if request.get("action") not in ACTIONS or request.get("target_harness") not in HARNESSES:
        raise BrokerError("broker-request-invalid", "action/target_harness")
    if request.get("depth") != 2 or not request.get("parent"):
        raise BrokerError("broker-request-invalid", "logical depth/parent")
    if int(request.get("fallback_ordinal", 0)) not in (1, 2):
        raise BrokerError("broker-request-invalid", "fallback_ordinal")
    worktree = absolute(request["worktree"], "worktree", must_exist=True)
    artifact_root = absolute(request["artifact_root"], "artifact_root", must_exist=True)
    request_jobs = absolute(request["jobs"], "jobs")
    if request_jobs != jobs.resolve(strict=False):
        raise BrokerError("broker-jobs-mismatch", f"expected={jobs} observed={request_jobs}")
    request_broker_root = absolute(request["broker_root"], "broker_root")
    if request_broker_root != broker_root.resolve(strict=False):
        raise BrokerError("broker-root-mismatch", f"expected={broker_root} observed={request_broker_root}")
    allowed_instances = allowed_broker_instances or {broker_instance}
    if request.get("broker_instance") not in allowed_instances:
        raise BrokerError(
            "broker-instance-mismatch",
            f"expected={sorted(allowed_instances)} observed={request.get('broker_instance')}",
        )
    if request.get("prompt_file"):
        absolute(request["prompt_file"], "prompt_file", must_exist=True)
    if request.get("parent_cwd"):
        absolute(request["parent_cwd"], "parent_cwd", must_exist=True)
    for field in (
        "slug", "capability", "mode", "intensity", "worker_role", "owner", "model_role",
        "route_id", "route_hash", "route_node", "registry_digest", "completion_gate",
        "parent_harness", "parent_transport", "parent_sandbox", "requested_launch_authority",
        "probe_source", "broker_instance",
    ):
        if not isinstance(request.get(field), str) or not request[field] or len(request[field]) > 4096:
            raise BrokerError("broker-request-invalid", field)
        if any(char in request[field] for char in ("\x00", "\r", "\n", "\t")):
            raise BrokerError("broker-request-invalid", f"{field} contains a control character")
    if request["parent_harness"] not in HARNESSES:
        raise BrokerError("broker-request-invalid", "parent_harness")
    validate_route(request)
    normalized = dict(request)
    normalized["worktree"] = str(worktree)
    normalized["artifact_root"] = str(artifact_root)
    normalized["jobs"] = str(request_jobs)
    normalized["route_file"] = str(absolute(request["route_file"], "route_file", must_exist=True))
    normalized["broker_root"] = str(request_broker_root)
    return normalized


def adapter_command(request: dict, instance_id: str) -> list[str]:
    harness = request["target_harness"]
    if harness == "claude":
        command = [sys.executable, str(ROOT / "adapters/claude/bin/dispatch-headless.py")]
    elif harness in {"codex", "opencode"}:
        command = [str(ROOT / f"adapters/{harness}/bin/preflight.sh"), "dispatch"]
    else:  # validate_request already rejects this; keep construction fail-closed.
        raise BrokerError("broker-target-unsupported", str(harness))
    command += [
        f"--{request['action']}",
        "--worktree", request["worktree"],
        "--slug", request["slug"],
        "--capability", request["capability"],
        "--mode", request["mode"],
        "--intensity", request["intensity"],
        "--depth", "2",
        "--parent", request["parent"],
        "--worker-role", request["worker_role"],
        "--owner", request["owner"],
        "--owner-harness", request["parent_harness"],
        "--model-role", request["model_role"],
        "--route-file", request["route_file"],
        "--route-id", request["route_id"],
        "--route-hash", request["route_hash"],
        "--route-node", request["route_node"],
        "--registry-digest", request["registry_digest"],
        "--write-scope", request["write_scope"],
        "--completion-gate", request["completion_gate"],
        "--jobs", request["jobs"],
        "--attempt-id", request["attempt_id"],
        "--broker-request-id", request["request_id"],
        "--parent-harness", request["parent_harness"],
        "--parent-transport", request["parent_transport"],
        "--parent-sandbox", request["parent_sandbox"],
        "--launch-authority", "ancestor-broker",
        "--nested-eligibility", "supported",
        "--eligibility-source", f"depth-0-broker:{instance_id}",
        "--eligibility-failure-class", request.get("probe_failure_class") or "-",
        "--fallback-ordinal", str(request["fallback_ordinal"]),
    ]
    for field, flag in (
        ("prompt_file", "--prompt-file"),
        ("parent_session_id", "--parent-session-id"),
        ("parent_cwd", "--parent-cwd"),
    ):
        if request.get(field):
            command += [flag, request[field]]
    return command


class BrokerServer:
    def __init__(
        self,
        root: Path,
        jobs: Path,
        instance_id: str,
        stale_seconds: float,
        predecessor_instance: str = "",
    ):
        self.root = root
        self.jobs = jobs.resolve(strict=False)
        self.instance_id = instance_id
        self.predecessor_instance = predecessor_instance
        self.stale_seconds = stale_seconds
        self.p = paths(root)
        self.pid = os.getpid()
        self.start_ticks = process_start_ticks(self.pid)
        self.stop_event = threading.Event()
        self.locks_guard = threading.Lock()
        self.request_locks: dict[str, threading.Lock] = {}
        self.meta_guard = threading.Lock()
        self.sock: socket.socket | None = None

    def request_lock(self, request_id: str) -> threading.Lock:
        with self.locks_guard:
            lock = self.request_locks.get(request_id)
            if lock is None:
                lock = self.request_locks[request_id] = threading.Lock()
            return lock

    def meta(self, state: str = "running") -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "implementation_digest": IMPLEMENTATION_DIGEST,
            "state": state,
            "instance_id": self.instance_id,
            "predecessor_instance": self.predecessor_instance,
            "fencing_token": self.instance_id,
            "pid": self.pid,
            "start_ticks": self.start_ticks,
            "jobs": str(self.jobs),
            "socket": str(self.p["socket"]),
            "heartbeat_epoch": time.time(),
            "heartbeat_at": utcnow(),
        }

    def write_meta(self, state: str = "running") -> None:
        with self.meta_guard:
            atomic_json(self.p["meta"], self.meta(state))

    def heartbeat(self) -> None:
        while not self.stop_event.wait(1.0):
            self.write_meta()

    def request_path(self, request_id: str) -> Path:
        return self.p["requests"] / f"{request_id}.json"

    def transition(self, state: dict, status: str, **fields: object) -> dict:
        if state.get("status") in TERMINAL:
            raise BrokerError("broker-terminal-state-immutable", str(state.get("status")))
        updated = dict(state)
        updated.update(fields)
        updated["status"] = status
        updated["updated_at"] = utcnow()
        atomic_json(self.request_path(updated["request_id"]), updated)
        return updated

    def recovered_response(self, state: dict, row: dict) -> dict:
        note = row["metadata"].get("note", "")
        output = "broker_recovered_from_registry=1"
        if note.startswith("dead-"):
            output += f"\nearly_death={note[5:]}"
        response = {
            "returncode": 0,
            "stdout": output,
            "stderr": "",
            "attempt_id": state["attempt_id"],
            "broker_pid": self.pid,
            "broker_start_ticks": self.start_ticks,
            "broker_instance": self.instance_id,
            "recovered_from_registry": True,
        }
        recovered = self.transition(state, "done", response=response, recovered_at=utcnow())
        return {"ok": True, "state": recovered}

    def renew_lease(self, request_id: str, stop_event: threading.Event) -> None:
        interval = max(0.5, self.stale_seconds / 3.0)
        while not stop_event.wait(interval):
            with self.request_lock(request_id):
                path = self.request_path(request_id)
                if not path.is_file():
                    return
                state = read_json(path)
                if state.get("status") in TERMINAL or state.get("broker_instance") != self.instance_id:
                    return
                state["lease_expires_epoch"] = time.time() + self.stale_seconds
                state["updated_at"] = utcnow()
                atomic_json(path, state)

    def process_request(self, request: dict) -> dict:
        request_id = str(request.get("request_id", ""))
        prior_recovery = (
            bool(self.predecessor_instance)
            and request.get("broker_instance") == self.predecessor_instance
            and bool(REQUEST_ID_RE.fullmatch(request_id))
            and self.request_path(request_id).is_file()
        )
        allowed_instances = {self.instance_id}
        if prior_recovery:
            allowed_instances.add(self.predecessor_instance)
        normalized = validate_request(
            request,
            self.jobs,
            self.root,
            self.instance_id,
            allowed_broker_instances=allowed_instances,
        )
        digest = "sha256:" + hashlib.sha256(canonical(normalized)).hexdigest()
        path = self.request_path(normalized["request_id"])

        with self.request_lock(normalized["request_id"]):
            if path.is_file():
                state = read_json(path)
                if state.get("request_hash") != digest:
                    raise BrokerError("broker-request-id-conflict", normalized["request_id"])
                if state.get("status") in TERMINAL:
                    return {"ok": True, "state": state, "duplicate": True}
                # inflight check first: while this instance still renews the
                # lease for a running target, a resubmit must be rejected
                # outright rather than reconciled against the registry.
                lease = float(state.get("lease_expires_epoch", 0.0) or 0.0)
                if state.get("broker_instance") == self.instance_id and lease > time.time():
                    raise BrokerError("broker-request-inflight", normalized["request_id"])
                # registry reconcile is for fenced recovery only (predecessor
                # instance or an expired lease) -- never for a live inflight
                # request, or a resubmit could observe the spawn-time registry
                # row and terminate the request while the target is still running.
                row = registry_attempt(self.jobs, state["attempt_id"])
                if row is not None:
                    return self.recovered_response(state, row)
                state["recovered_after_fence"] = True
                state["prior_lease_expires_epoch"] = lease
            else:
                state = {
                    "schema_version": SCHEMA_VERSION,
                    "request_id": normalized["request_id"],
                    "attempt_id": normalized["attempt_id"],
                    "request_hash": digest,
                    "request": normalized,
                    "status": "queued",
                    "created_at": utcnow(),
                    "updated_at": utcnow(),
                }
                atomic_json(path, state)
            state = self.transition(
                state,
                "claimed",
                broker_instance=self.instance_id,
                fencing_token=self.instance_id,
                broker_pid=self.pid,
                broker_start_ticks=self.start_ticks,
                lease_expires_epoch=time.time() + self.stale_seconds,
            )
            command = adapter_command(normalized, self.instance_id)
            state = self.transition(state, "running", launched_command_harness=normalized["target_harness"])
            env = {
                **os.environ,
                "AGENT_ARTIFACT_ROOT": normalized["artifact_root"],
                "AGENT_DISPATCH_JOBS": str(self.jobs),
                "AGENT_DISPATCH_BROKER_ROOT": str(self.root),
                "AGENT_DISPATCH_BROKER_INSTANCE": self.instance_id,
                "AGENT_DISPATCH_LAUNCH_AUTHORITY": "ancestor-broker",
            }

        renew_stop = threading.Event()
        renewer = threading.Thread(
            target=self.renew_lease, args=(normalized["request_id"], renew_stop), daemon=True
        )
        renewer.start()
        try:
            result = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True, check=False)
        finally:
            renew_stop.set()
            renewer.join(timeout=2.0)
        response = {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "attempt_id": normalized["attempt_id"],
            "broker_pid": self.pid,
            "broker_start_ticks": self.start_ticks,
            "broker_instance": self.instance_id,
            "recovered_from_registry": False,
        }
        terminal = "done" if result.returncode == 0 else "failed"
        with self.request_lock(normalized["request_id"]):
            current = read_json(path)
            if current.get("status") in TERMINAL:
                return {"ok": True, "state": current}
            state = self.transition(current, terminal, response=response, terminal_at=utcnow())
            return {"ok": True, "state": state}

    def handle(self, conn: socket.socket) -> None:
        try:
            message = recv_json(conn)
            op = message.get("op")
            if op == "ping":
                send_json(conn, {"ok": True, "meta": self.meta()})
                return
            if op == "shutdown":
                if message.get("instance_id") != self.instance_id:
                    raise BrokerError("broker-instance-mismatch", "shutdown")
                self.stop_event.set()
                send_json(conn, {"ok": True, "state": "stopping"})
                return
            if op != "request" or not isinstance(message.get("request"), dict):
                raise BrokerError("broker-request-invalid", "unknown operation")
            send_json(conn, self.process_request(message["request"]))
        except BrokerError as exc:
            send_json(conn, response_error(exc))
        except Exception as exc:  # keep the broker alive but fail this request closed.
            send_json(conn, {"ok": False, "reason": "broker-internal-error", "detail": str(exc)})
        finally:
            conn.close()

    def serve(self) -> int:
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.p["requests"].mkdir(parents=True, exist_ok=True)
        lock = self.p["lock"].open("a+", encoding="utf-8")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 73
        try:
            if self.p["socket"].exists():
                self.p["socket"].unlink()
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.bind(str(self.p["socket"]))
            os.chmod(self.p["socket"], 0o600)
            self.sock.listen(16)
            self.sock.settimeout(0.5)
            self.write_meta()
            thread = threading.Thread(target=self.heartbeat, name="dispatch-broker-heartbeat", daemon=True)
            thread.start()
            while not self.stop_event.is_set():
                try:
                    conn, _ = self.sock.accept()
                except socket.timeout:
                    continue
                worker = threading.Thread(target=self.handle, args=(conn,), daemon=True)
                worker.start()
            self.write_meta("stopped")
            return 0
        finally:
            self.stop_event.set()
            if self.sock is not None:
                self.sock.close()
            if self.p["socket"].exists():
                self.p["socket"].unlink()
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()


def connect(root: Path, payload: dict, timeout: float) -> dict:
    p = paths(root)
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    try:
        conn.connect(str(p["socket"]))
        send_json(conn, payload)
        return recv_json(conn)
    except socket.timeout as exc:
        raise BrokerError("broker-timeout", str(exc)) from exc
    except OSError as exc:
        raise BrokerError("broker-unavailable", str(exc)) from exc
    finally:
        conn.close()


def ensure(root: Path, jobs: Path, stale_seconds: float, timeout: float) -> dict:
    if os.environ.get("AGENT_SESSION_ROLE") == "worker" or os.environ.get("AGENT_DISPATCH_CHILD") == "1":
        raise BrokerError("broker-ensure-worker-forbidden", "only depth 0 may prepare the broker")
    replacement_reason = ""
    try:
        return broker_status(root, jobs, stale_seconds)
    except BrokerError as exc:
        replacement_reason = exc.reason
        if exc.reason not in {
            "broker-unavailable",
            "broker-stale",
            "broker-jobs-mismatch",
            "broker-instance-mismatch",
            "broker-implementation-mismatch",
        }:
            raise
    # A new depth-0 dispatch may deliberately bind an isolated fixture jobs
    # registry.  Rotate an older responsive broker instead of letting its global
    # process silently retain the previous authority.  No signal-by-PID fallback:
    # shutdown must prove the fenced instance over its socket.
    meta_path = paths(root)["meta"]
    predecessor_instance = ""
    if meta_path.is_file():
        try:
            old = read_json(meta_path)
            predecessor_instance = str(old.get("instance_id", ""))
            old_pid = int(old.get("pid", 0) or 0)
            old_ticks = str(old.get("start_ticks", ""))
            if process_matches(old_pid, old_ticks):
                active = []
                for request_path in paths(root)["requests"].glob("req-*.json"):
                    request_state = read_json(request_path)
                    if request_state.get("status") not in TERMINAL:
                        active.append(request_state.get("request_id", request_path.stem))
                if active:
                    raise BrokerError("broker-replacement-active", ",".join(sorted(active)))
                if old.get("state") == "stopped":
                    deadline = time.monotonic() + timeout
                    while process_matches(old_pid, old_ticks) and time.monotonic() < deadline:
                        time.sleep(0.05)
                    if process_matches(old_pid, old_ticks):
                        raise BrokerError("broker-replacement-timeout", replacement_reason)
                else:
                    reply = connect(
                        root,
                        {"op": "shutdown", "instance_id": old.get("instance_id")},
                        min(2.0, timeout),
                    )
                    if not reply.get("ok"):
                        raise BrokerError("broker-replacement-refused", replacement_reason)
                    deadline = time.monotonic() + timeout
                    while process_matches(old_pid, old_ticks) and time.monotonic() < deadline:
                        time.sleep(0.05)
                    if process_matches(old_pid, old_ticks):
                        raise BrokerError("broker-replacement-timeout", replacement_reason)
        except BrokerError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise BrokerError("broker-replacement-refused", str(exc)) from exc
    root.mkdir(parents=True, exist_ok=True)
    p = paths(root)
    instance_id = "brk-" + uuid.uuid4().hex
    command = [
        sys.executable, str(Path(__file__).resolve()), "serve",
        "--root", str(root), "--jobs", str(jobs), "--instance-id", instance_id,
        "--stale-seconds", str(stale_seconds),
    ]
    if predecessor_instance:
        command += ["--predecessor-instance", predecessor_instance]
    with p["log"].open("a", encoding="utf-8") as log:
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    deadline = time.monotonic() + timeout
    last: BrokerError | None = None
    while time.monotonic() < deadline:
        time.sleep(0.05)
        try:
            meta = broker_status(root, jobs, stale_seconds)
            if meta.get("instance_id") == instance_id:
                return meta
            raise BrokerError("broker-instance-mismatch", f"expected={instance_id} observed={meta.get('instance_id')}")
        except BrokerError as exc:
            last = exc
    raise BrokerError("broker-start-timeout", last.detail if last else str(root))


def print_meta(meta: dict) -> None:
    print("check=ok")
    print(f"broker_root={Path(meta['socket']).parent}")
    print(f"broker_instance={meta['instance_id']}")
    print(f"broker_pid={meta['pid']}")
    print(f"broker_start_ticks={meta['start_ticks']}")
    print(f"broker_jobs={meta['jobs']}")
    print(f"broker_implementation={meta['implementation_digest']}")
    print(f"broker_heartbeat={meta['heartbeat_at']}")


def parse_common(sub: argparse.ArgumentParser, *, jobs: bool = True) -> None:
    sub.add_argument("--root", type=Path, default=default_root())
    if jobs:
        sub.add_argument("--jobs", type=Path, required=True)
    sub.add_argument("--stale-seconds", type=float, default=DEFAULT_STALE_SECONDS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    ensure_p = commands.add_parser("ensure")
    parse_common(ensure_p)
    ensure_p.add_argument("--timeout", type=float, default=5.0)
    status_p = commands.add_parser("status")
    parse_common(status_p)
    serve_p = commands.add_parser("serve")
    parse_common(serve_p)
    serve_p.add_argument("--instance-id", required=True)
    serve_p.add_argument("--predecessor-instance", default="")
    request_p = commands.add_parser("request")
    parse_common(request_p)
    request_p.add_argument("--timeout", type=float, default=45.0)
    stop_p = commands.add_parser("stop")
    parse_common(stop_p)
    stop_p.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()
    if args.command in {"ensure", "request", "serve"}:
        print("check=failed")
        print("reason=launch-broker-retired")
        print("detail=dispatch contract v3 permits legacy broker status and stop only")
        return 76
    root = args.root.expanduser().resolve(strict=False)
    jobs = args.jobs.expanduser().resolve(strict=False)
    try:
        if args.command == "ensure":
            print_meta(ensure(root, jobs, args.stale_seconds, args.timeout))
            return 0
        if args.command == "status":
            meta = broker_status(root, jobs, args.stale_seconds)
            reply = connect(root, {"op": "ping"}, min(2.0, args.stale_seconds))
            if not reply.get("ok") or reply.get("meta", {}).get("instance_id") != meta.get("instance_id"):
                raise BrokerError("broker-instance-mismatch", "ping metadata")
            print_meta(meta)
            print("broker_lifecycle=retired")
            return 0
        if args.command == "serve":
            server = BrokerServer(
                root,
                jobs,
                args.instance_id,
                args.stale_seconds,
                args.predecessor_instance,
            )
            signal.signal(signal.SIGTERM, lambda *_: server.stop_event.set())
            signal.signal(signal.SIGINT, lambda *_: server.stop_event.set())
            return server.serve()
        meta = broker_status(root, jobs, args.stale_seconds)
        if args.command == "request":
            try:
                request = json.load(sys.stdin)
            except json.JSONDecodeError as exc:
                raise BrokerError("broker-request-invalid", str(exc)) from exc
            reply = connect(root, {"op": "request", "request": request}, args.timeout)
            print(json.dumps(reply, ensure_ascii=False, sort_keys=True))
            if not reply.get("ok"):
                return 76
            return 0
        reply = connect(root, {"op": "shutdown", "instance_id": meta["instance_id"]}, args.timeout)
        if not reply.get("ok"):
            raise BrokerError(str(reply.get("reason", "broker-stop-failed")), str(reply.get("detail", "")))
        print("check=ok")
        print("broker_state=stopping")
        print(f"broker_instance={meta['instance_id']}")
        return 0
    except BrokerError as exc:
        print("check=failed")
        print(f"reason={exc.reason}")
        print(f"detail={exc.detail}")
        return 76


if __name__ == "__main__":
    raise SystemExit(main())
