#!/usr/bin/env python3
"""Detached process runner with PID reuse-safe reattachment."""
import argparse, contextlib, fcntl, hashlib, json, os, re, select, signal, stat, subprocess, sys, time
from pathlib import Path
import resource_resume as RESOURCE_RESUME
import resource_run_evidence as RUN_EVIDENCE
from resource_progress import environment as progress_environment
import gpu_leases
import route_authority
from gpu_execution_sandbox import gpu_resource_nodes
from resource_run_registry import (
    classify_identity,
    is_alive,
    launched_identity,
    proc_identity,
    register_registry,
    resource_never_started,
    reboot_evidence,
)

SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
RETRY = re.compile(r"^(?P<base>.+)__a(?P<n>[1-9][0-9]*)$")
TERMINAL_STATUSES = {"succeeded", "failed"}

# The payload runs under a tiny shell sentinel wrapper so its exit status survives
# every observer. A detached run whose launcher, supervisor, and session are all gone
# must still be able to prove *how* it ended; without this, "the process is not there"
# is indistinguishable from "the process finished successfully" (2026-08-04 BC_ResNet_tf).
SENTINEL_SCRIPT = (
    '"$@"; ec=$?; '
    'printf %s "$ec" > "$AGENT_RESOURCE_SENTINEL.partial" 2>/dev/null && '
    'mv "$AGENT_RESOURCE_SENTINEL.partial" "$AGENT_RESOURCE_SENTINEL" 2>/dev/null; '
    'if [ -n "${HEARTING_GPU_LEASE_RELEASE:-}" ]; then /bin/sh -c "$HEARTING_GPU_LEASE_RELEASE"; fi; '
    'exit $ec'
)

def alive(run):
    return is_alive(run)
def fail(message):
    print("resource-runner:", message, file=sys.stderr)
    error = SystemExit(65)
    error.resource_message = message
    raise error


def fail_compute(reason, *, differences=(), next_action="start the resource through the existing route",
                 retryable=False):
    record = {"reason": reason, "differences": list(differences),
              "next_action": next_action, "retryable": bool(retryable)}
    message = json.dumps(record, ensure_ascii=False, sort_keys=True)
    print("resource-runner: " + message, file=sys.stderr)
    error = SystemExit(65)
    error.resource_message = message
    raise error


def compute_output_refusal(reason):
    """Keep absent evidence distinct from changed or unsafe output snapshots."""
    if reason in {"compute-run-output-missing", "compute-run-output-base-unavailable",
                  "compute-run-output-tree-unavailable"}:
        return "resource-compute-output-missing"
    return "resource-compute-output-mismatch"


def gpu_payload_environment(environment, lease):
    """Use the lease's exact session for GPU payloads; leave CPU paths untouched."""
    if not lease:
        return environment
    return gpu_leases.launcher_session_environment(environment, owner=lease.get("owner"))


def locked_update(path, fn):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    with open(str(path)+".lock","a+") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX); data=json.loads(path.read_text()) if path.exists() else {"schema_version":1,"runs":{}}
        result=fn(data); tmp=path.with_suffix(path.suffix+".tmp"); tmp.write_text(json.dumps(data,indent=2)+"\n"); os.replace(tmp,path); return result

def read_sentinel(path):
    """Return the payload's recorded exit code, or None when it left no proof."""
    if not path:
        return None
    try:
        raw=Path(path).read_text(encoding="utf-8",errors="replace").strip()
    except OSError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None

def settle(registry, run_id, run, *, data=None):
    """Persist the terminal row once the process is verifiably gone.

    Any observer may call this — `reap`, `status`, `list`, a continuation supervisor, or
    a Fleet-adjacent status pass — and they converge on the same row. A stored
    `running` that outlives its process is a defect, not a state, so termination is
    recorded by whoever notices it first and is idempotent afterwards.
    """
    if resource_never_started(run):
        def settle_unstarted(data):
            row = data["runs"].get(run_id)
            if row != run:
                raise ValueError("resource-reservation-changed")
            if row.get("ended_at") is not None:
                return row, False
            row.update(ended_at=time.time(), exit_code=None)
            return row, True
        return settle_unstarted(data) if data is not None else locked_update(registry, settle_unstarted)
    liveness,_current,reason=classify_identity(run)
    if liveness in {"working", "reaping"} or (run.get("resource_policy") in {"verified-resume", "supervised-owner"}
                              and run.get("status") == "launching"):
        return run, False
    exit_code=read_sentinel(run.get("sentinel"))
    if run.get("resource_policy") in {"verified-resume", "supervised-owner"} and run.get("cancel_requested") is True:
        status,state,failure="failed","CANCELLED","cancelled"
    elif reason == 'host-reboot' and exit_code is None:
        status,state,failure="failed","FAILED_RETRYABLE","host-reboot"
    elif run.get("resource_policy") in {"verified-resume", "supervised-owner"} and liveness != "exited":
        status,state,failure="failed","FAILED_RETRYABLE",reason
    elif exit_code==0:
        status,state,failure="succeeded","STAGE_SUCCEEDED",None
    elif exit_code is not None:
        status,state,failure="failed","FAILED_RETRYABLE",f"exit-{exit_code}"
    elif liveness=="stale":
        # PID reuse or an identity mismatch: the recorded process is gone, and it left
        # no exit proof. Absence of evidence is never success.
        status,state,failure="failed","FAILED_RETRYABLE",reason
    else:
        status,state,failure="failed","FAILED_RETRYABLE","no-exit-sentinel"
    def apply(data):
        row=data["runs"].get(run_id)
        if row is None: raise ValueError("unknown run id")
        if row.get("status") in TERMINAL_STATUSES:
            return row, False
        if row != run:
            raise ValueError('resource-reservation-changed')
        row.update({"status":status,"exit_code":exit_code,"ended_at":time.time(),
                    "workflow_state":state,"failure_class":failure,
                    "liveness_reason":reason})
        if failure == 'host-reboot':
            boot = reboot_evidence(run)
            if boot:
                # Retain the observed old boot so compatibility evidence does
                # not expire when this kernel's uptime grows past old ticks.
                row.update(boot_id=boot['previous_boot_id'], boot_host=boot['boot_host'])
        return row, True
    return apply(data) if data is not None else locked_update(registry,apply)


class LaunchDeferred(Exception):
    """An actual queued correction takes precedence over unreleased payloads."""


def launch_request(args):
    request = {}
    for name in ("smoke_attestation", "config_manifest"):
        value = getattr(args, name, None)
        request[name] = str(Path(value).resolve(strict=True)) if value else None
        request[name + "_sha256"] = hashlib.sha256(Path(value).read_bytes()).hexdigest() if value else None
    return request


def controller_argv(registry, row):
    request = row["launch_request"]
    argv = ["--registry", str(registry), "start", "--run-id", row["run_id"],
            "--cwd", row["cwd"], "--log", row["log"], "--route", row["route"],
            "--node", row["node"], "--parent-attempt-id", row["parent_attempt_id"],
            "--jobs", row["jobs"]]
    for name in ("smoke_attestation", "config_manifest"):
        if request[name]:
            argv += ["--" + name.replace("_", "-"), request[name]]
    if row.get("share"):
        argv += ["--share"]
    return argv + ["--", *row["command"]]


def start_watch(route_file, jobs, cwd, runtime):
    """Start only the observer, confirming readiness before publishing its identity."""
    ready_read, ready_write = os.pipe()
    watch = None
    try:
        runtime.mkdir(parents=True, exist_ok=True)
        with open(runtime / "watch.log", "ab", buffering=0) as output:
            watch = subprocess.Popen([sys.executable, str(Path(__file__).with_name("workflow-supervisor.py")),
                "watch", "--route", str(route_file), "--jobs", str(jobs), "--interval", "1",
                "--ready-fd", str(ready_write)],
                env={**os.environ, "AGENT_DISPATCH_JOBS": str(jobs)}, cwd=cwd,
                pass_fds=(ready_write,), stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        os.close(ready_write)
        ready_write = None
        if not select.select([ready_read], [], [], 5)[0] or os.read(ready_read, 32) != b"ready\n":
            raise ValueError("resource-supervisor-start-unconfirmed")
        supervision = proc_identity(watch.pid)
        if not supervision or watch.poll() is not None:
            raise ValueError("resource-supervisor-exited-before-launch")
        return watch, supervision
    except Exception:
        # Only our newly created observer handle is ours to stop.
        if watch is not None and watch.poll() is None:
            watch.terminate()
            watch.wait(timeout=5)
        raise
    finally:
        os.close(ready_read)
        if ready_write is not None:
            os.close(ready_write)


def start_verified(registry, args, route, route_file, placeholder, *, controller=None):
    """Own the existing watch before releasing an exact, once-only payload."""
    from artifact_producer import prepare_route_artifact_env
    from dispatch_contract import resolve_agent_home, resolve_dispatch_state_root
    import workflow_state as WS
    jobs = Path(args.jobs or os.environ.get("AGENT_DISPATCH_JOBS") or
                resolve_dispatch_state_root(resolve_agent_home()) / "jobs.log").resolve(strict=True)
    inherited_jobs = os.environ.get("AGENT_DISPATCH_JOBS")
    if inherited_jobs and Path(inherited_jobs).resolve(strict=True) != jobs:
        raise ValueError("resource-dispatch-jobs-conflict")
    owner_wait = placeholder.get("owner_wait")
    placeholder.update(resource_policy="supervised-owner" if owner_wait else "verified-resume",
                       route=str(route_file), jobs=str(jobs))
    if owner_wait:
        request = launch_request(args)
        if controller is not None and request != controller.expected.get("launch_request"):
            fail("resource-launch-request-changed")
        placeholder["launch_request"] = request
    keys = ("run_id", "cwd", "log", "command", "route", "node", "parent_attempt_id", "jobs",
            "config_ref", "config_sha256", "source_commit", "source_dirty", "source_git_state", "config_layout",
            "resource_policy", "owner_wait", "launch_request", "share")
    import dispatch_resource_wait as OWNER_RESOURCE
    import resource_run_registry as RR
    sup = OWNER_RESOURCE.supervisor()
    ledger = sup.ledger_for(route, jobs) if route.get("route_id") else None
    # Capture compute predecessor evidence before taking the ledger or registry
    # writer lock. The callback below rechecks each captured row byte identity.
    try:
        initial_registry = json.loads(Path(registry).read_text(encoding="utf-8"))
    except FileNotFoundError:
        initial_registry = {"runs": {}}
    initial_matches = [row for row in (initial_registry.get("runs") or {}).values()
                       if row.get("route") == str(route_file) and row.get("node") == args.node]
    initial_history_pairs = sup.resource_predecessors(ledger, args.node) if ledger else []
    initial_history = [row for _, row in initial_history_pairs]
    captured_compute = {}
    for row in [*initial_matches, *initial_history]:
        execution = row.get("execution") if isinstance(row, dict) else None
        if isinstance(execution, dict) and execution.get("kind") == "compute-hosts":
            captured_compute[row.get("run_id")] = {
                "row": row, "state": RR.compute_execution_observation(row).get("state")}
    for armed_row, row in initial_history_pairs:
        if row.get("run_id") in captured_compute:
            captured_compute[row["run_id"]]["armed"] = armed_row
    def reserve(data):
        if controller is not None:
            current = data["runs"].get(args.run_id)
            if (current != controller.expected or current.get("launch_state") != "queued"
                    or any(current.get(k) != placeholder.get(k) for k in keys)):
                raise ValueError("resource-reservation-changed")
            claimed = {**current, "launch_state": "claimed", "launch_controller": controller.identity}
            data["runs"][args.run_id] = claimed
            return True, claimed
        matches = [row for row in data["runs"].values() if row.get("route") == str(route_file)
                   and row.get("node") == args.node]
        for row in matches:
            execution = row.get("execution") if isinstance(row, dict) else None
            if isinstance(execution, dict) and execution.get("kind") == "compute-hosts":
                captured = captured_compute.get(row.get("run_id"))
                if not captured or captured["row"] != row:
                    raise ValueError("resource-reservation-changed")
        current = data["runs"].get(args.run_id)
        if current is not None:
            if any(current.get(k) != placeholder.get(k) for k in keys):
                raise ValueError("resource-route-body-conflict")
            return False, current
        for previous in matches:
            if reboot_evidence(previous):
                settle(registry, previous['run_id'], previous, data=data)
        from route_authority import require_resource_predecessors
        if captured_compute:
            require_resource_predecessors([*matches, *history], placeholder,
                                          observations=captured_compute)
        else:
            require_resource_predecessors([*matches, *history], placeholder)
        data["runs"][args.run_id] = placeholder
        return True, placeholder
    with ledger.lock() if ledger else contextlib.nullcontext():
        if ledger and sup.resource_continuation_cancelled(route, ledger):
            raise ValueError("resource-parent-close-requested")
        history_pairs = sup.resource_predecessors(ledger, args.node) if ledger else []
        history = [row for _, row in history_pairs]
        for armed_row, row in history_pairs:
            execution = row.get("execution") if isinstance(row, dict) else None
            if isinstance(execution, dict) and execution.get("kind") == "compute-hosts":
                captured = captured_compute.get(row.get("run_id"))
                if (not captured or captured["row"] != row
                        or captured.get("armed") != armed_row):
                    raise ValueError("resource-reservation-changed")
        with controller.guard() if controller else contextlib.nullcontext():
            created, row = locked_update(registry, reserve)
    if not created:
        armed = sup.read_armed(ledger).get(args.node)
        if armed and armed.get("predecessor_id") == args.run_id:
            sup.reattach_resource_watch(route, ledger, armed)
            row = json.loads(Path(registry).read_text())["runs"][args.run_id]
        print(json.dumps({**row, "replayed": True, "payload_spawned": False,
                          **RUN_EVIDENCE.paths(armed or {}),
                          "supervisor_alive": RESOURCE_RESUME.supervisor_alive(row.get("supervision"))}))
        return
    placeholder = row
    proc = None
    release = None
    watch = None
    payload_released = False
    lease_path, gpu_lease = None, None
    try:
        register_registry(registry)
        artifacts = prepare_route_artifact_env(route_file, start=True, jobs=jobs)
        node = next((n for n in route.get("nodes", []) if n["id"] == args.node), {})
        # Native Codex tools record intent; their outer controller admits the
        # devices before arming/releasing the actual execution.
        queued_intent = (owner_wait and owner_wait.get("launch_scope") == "codex-owner-controller"
                         and controller is None)
        if not queued_intent:
            lease_path, gpu_lease, gpu_env = gpu_leases.resource_admission(
                node, placeholder["command"], gpu_scoped=args.node in gpu_resource_nodes(route),
                share=getattr(args, "share", False), jobs=jobs, run_id=args.run_id)
        output_receipt = RUN_EVIDENCE.paths({"artifact_base": artifacts["AGENT_ARTIFACT_OUTPUT_DIR"],
            "declared_outputs": node.get("outputs", ["run.json"]),
            "route_file": str(route_file), "route_id": route.get("route_id"),
            "route_hash": route.get("route_hash"), "node": args.node}, prepare=True)
        ledger = WS.WorkflowLedger(route["route_id"], route["route_hash"], jobs=jobs)
        runtime = ledger.root / "resource"
        runtime.mkdir(parents=True, exist_ok=True)
        successor = [sys.executable, str(Path(__file__).with_name("capability-route.py")),
                     "start", "--route", str(route_file), "--jobs", str(jobs)]
        supervisor = str(Path(__file__).with_name("workflow-supervisor.py"))
        environment = {**os.environ, "AGENT_DISPATCH_JOBS": str(jobs)}
        continuation = (["--successor-external"] if owner_wait else
                        ["--successor-command", json.dumps(successor)])
        subprocess.run([sys.executable, supervisor, "arm", "--route", str(route_file),
            "--node", args.node, "--predecessor-kind", "resource", "--predecessor-id", args.run_id,
            "--resource-registry", str(registry), "--jobs", str(jobs),
            "--artifact-base", artifacts["AGENT_ARTIFACT_OUTPUT_DIR"],
            *continuation, "--successor-cwd", str(placeholder["cwd"]),
            "--successor-log", str(runtime / "verification-start.log")],
            check=True, env=environment, stdout=subprocess.DEVNULL, timeout=30)
        with ledger.lock():
            if sup.resource_continuation_cancelled(route, ledger):
                raise ValueError("resource-parent-close-requested")
        if queued_intent:
            queued = {**placeholder, "launch_state": "queued"}
            publish_verified_run(registry, args.run_id, placeholder, queued)
            print(json.dumps({**queued, "payload_spawned": False, "supervisor_alive": False,
                              **output_receipt,
                              "required_action": "yield-owner-turn", "verification_admitted": False,
                              "workflow_complete": False}))
            return
        watch, supervision = start_watch(route_file, jobs, placeholder["cwd"], runtime)
        log = Path(placeholder["log"])
        log.parent.mkdir(parents=True, exist_ok=True)
        sentinel = Path(placeholder["sentinel"])
        # Only this newly reserved run owns these paths; replays never unlink them.
        for path in (sentinel, Path(str(sentinel) + ".partial")):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
        wait_read, release = os.pipe()
        # Bash is required because retained generation descriptors can be multi-digit;
        # dash rejects dynamic fd duplication for descriptors >= 10.
        # -p skips ambient Bash startup files/functions/options before the release
        # fence, while leaving the payload's environment intact.
        launch_argv = ["/bin/bash", "-p", "-c", 'IFS= read -r launch <&"$AGENT_RESOURCE_LAUNCH_FD" || exit 125; '
                       + SENTINEL_SCRIPT, "resource-runner", *(controller.command if controller else placeholder["command"])]
        environment.update(progress_environment(placeholder))
        environment.update(HEARTING_GPU_LEASE_RELEASE="")
        environment.update(gpu_env)
        environment.update(HEARTING_RESOURCE_RUN_ID=args.run_id, HEARTING_RESOURCE_REGISTRY=str(registry.resolve()))
        environment.update(AGENT_RESOURCE_SENTINEL=str(sentinel), AGENT_RESOURCE_LAUNCH_FD=str(wait_read))
        environment = gpu_payload_environment(environment, gpu_lease)
        try:
            with open(log, "ab", buffering=0) as output:
                proc = subprocess.Popen(launch_argv, cwd=placeholder["cwd"], env=environment,
                    pass_fds=(wait_read,), stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        finally:
            os.close(wait_read)
        ident = launched_identity(proc)
        if not ident or not RESOURCE_RESUME.supervisor_alive(supervision):
            raise ValueError("resource-launch-identity-unconfirmed")
        if gpu_lease:
            gpu_leases.bind(lease_path, gpu_lease, gpu_leases.identity(proc.pid))
        row = {**placeholder, **ident, "pid_namespace": os.readlink("/proc/self/ns/pid"), "process_group": os.getpgid(proc.pid), "launch_argv": launch_argv,
               "status": "running", "workflow_state": "RUNNING", "supervision": supervision}
        if gpu_lease:
            row.update(gpu_lease=gpu_lease, gpus=",".join(gpu_lease["gpus"]))
        if controller is not None:
            row.update(launch_state="started", pid_namespace=controller.identity["pid_namespace"],
                       payload_sandbox=controller.sandbox)
        with ledger.lock():
            if sup.resource_continuation_cancelled(route, ledger):
                raise ValueError("resource-parent-close-requested")
            with controller.guard() if controller else contextlib.nullcontext():
                publish_verified_run(registry, args.run_id, placeholder, row)
                os.write(release, b"start\n")
                payload_released = True
        if controller is not None:
            # The outer controller keeps the actual Popen handles so it can
            # reap its children before observing /proc, without PID guessing.
            controller.children = (proc, watch)
            controller.row = row
        os.close(release)
        release = None
        print(json.dumps({**row, "replayed": False, "payload_spawned": True,
                          **output_receipt,
                          "supervisor_alive": True, "watch_seconds": None,
                          "verification_admitted": False, "workflow_complete": False}))
    except Exception as error:
        # Closing the private fence cannot run the payload. Do not signal a foreign PID.
        if release is not None:
            os.close(release)
        if proc is not None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=2)
        if not payload_released:
            gpu_leases.release(lease_path, gpu_lease)
            if watch is not None and watch.poll() is None:
                # This is our unreaped child, not a PID rediscovered in a registry.
                with contextlib.suppress(ProcessLookupError):
                    watch.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    watch.wait(timeout=2)
            def mark_failed(data):
                current = data["runs"].get(args.run_id)
                if current != placeholder and current != row:
                    raise ValueError("resource-reservation-changed")
                if isinstance(error, LaunchDeferred) and controller is not None:
                    data["runs"][args.run_id] = controller.expected
                else:
                    current.update(status="failed", workflow_state="FAILED_RETRYABLE",
                                   failure_class="resource-launch-incomplete",
                                   launch_state="not-started", ended_at=time.time(), exit_code=None,
                                   launch_error=launch_error(error))
                return current
            failed = locked_update(registry, mark_failed)
            if ledger and resource_never_started(failed):
                # Settle only this exact armed run through the normal failure
                # consumer, without polling or starting unrelated successors.
                with ledger.lock():
                    armed = sup.read_armed(ledger).get(args.node)
                    if (armed and armed.get("predecessor_id") == args.run_id
                            and not sup.resource_continuation_cancelled(route, ledger)):
                        sup._evaluate(route, ledger, armed, [])
            decision = route_authority.decide_continuation(action="resource-failure", facts={
                "not_started": resource_never_started(failed), "deferred": isinstance(error, LaunchDeferred)})
            if decision.allowed:
                print(json.dumps({**failed, "payload_spawned": False, "workflow_complete": False}))
                return
        raise


def sealed_compute_command(command):
    """Read only explicit compute-hosts contract fields from the sealed argv."""
    if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
        return None
    try:
        run_at = command.index("run")
        separator = command.index("--", run_at + 1)
    except ValueError:
        return None
    values = {"cwd": None, "env": None, "share": False}
    index = run_at + 1  # the host positional is placement and may vary
    if index >= separator or command[index].startswith("--"):
        return None
    index += 1
    while index < separator:
        part = command[index]
        if part.startswith("--cwd="):
            values["cwd"] = part.split("=", 1)[1]
        elif part == "--cwd" and index + 1 < separator:
            index += 1
            values["cwd"] = command[index]
        elif part.startswith("--env="):
            values["env"] = part.split("=", 1)[1]
        elif part == "--env" and index + 1 < separator:
            index += 1
            values["env"] = command[index]
        elif part == "--share":
            values["share"] = True
        elif part.startswith("--gpus="):
            pass
        elif part == "--gpus":
            index += 1
            if index >= separator:
                return None
        elif part.startswith("--name="):
            pass
        elif part == "--name":
            index += 1
            if index >= separator:
                return None
        elif part == "--json":
            pass
        else:
            return None
        index += 1
    if not values["cwd"] or not values["env"]:
        return None
    values["command"] = command[separator + 1:]
    return values if values["command"] else None


def adopt_compute_run(registry, args, route, route_file, compute_run_id):
    """Attach one evidenced compute-hosts execution to its same-route retry."""
    import dispatch_resource_wait as OWNER_RESOURCE
    import importlib.util
    import workflow_state as WS
    from resource_run_registry import compute_execution_identity
    from dispatch_owner_input import _locked as owner_input_lock, _target as owner_input_target

    registry = Path(registry).resolve(strict=True)
    jobs = Path(args.jobs or os.environ.get("AGENT_DISPATCH_JOBS", "")).resolve(strict=True)
    compute_path = Path(__file__).with_name("compute-hosts.py")
    compute_spec = importlib.util.spec_from_file_location("resource_runner_compute_hosts", compute_path)
    if compute_spec is None or compute_spec.loader is None:
        fail_compute("resource-compute-observation-unavailable")
    COMPUTE = importlib.util.module_from_spec(compute_spec)
    compute_spec.loader.exec_module(COMPUTE)
    sup = OWNER_RESOURCE.supervisor()
    try:
        config = COMPUTE.load_config()
        candidate = COMPUTE.execution_record(config, compute_run_id)
    except (OSError, ValueError, KeyError, TypeError, COMPUTE.ConfigError) as error:
        fail_compute("resource-compute-observation-unavailable",
                     differences=[{"field": "compute-run", "expected": "readable sealed run evidence",
                                   "actual": type(error).__name__}],
                     next_action="restore access to the configured compute-run evidence")
    if candidate.get("state") != "quiescent":
        reason = candidate.get("reason") or "compute-run-observation-unavailable"
        if reason.startswith("compute-run-output-"):
            token = compute_output_refusal(reason)
        elif "output" in reason:
            token = "resource-compute-output-missing"
        elif candidate.get("state") == "not-quiescent":
            token = "resource-compute-not-quiescent"
        elif "exit" in reason or "stopped" in reason:
            token = "resource-compute-exit-not-zero" if "exit" in reason else "resource-compute-stopped"
        else:
            token = "resource-compute-observation-unavailable"
        fail_compute(token, differences=(candidate.get("differences") or [
                         {"field": "compute-run.observation", "expected": "quiescent",
                          "actual": reason}]),
                     next_action=(candidate.get("next_action")
                                  or "wait for a fresh supported compute-host observation"),
                     retryable=candidate.get("retryable", reason in {
                         "compute-run-process-group-populated",
                         "compute-run-observation-unavailable",
                         "compute-run-host-generation-changed"}))
    execution = candidate["execution"]

    def require_current_compute_snapshot():
        differences_reader = getattr(COMPUTE, "run_snapshot_differences", None)
        if callable(differences_reader):
            differences = differences_reader(config, compute_run_id, execution)
        elif COMPUTE.run_snapshot_current(config, compute_run_id, execution):
            differences = []
        else:
            differences = [{"field": "compute_run.snapshot_current",
                            "expected": True, "actual": False}]
        if not differences:
            return
        output_fields = {"declared_outputs", "execution.outputs", "compute_run.outputs_sha256"}
        output_differences = [item for item in differences
                              if item.get("field") in output_fields
                              or str(item.get("field", "")).startswith("outputs.")]
        if output_differences:
            fail_compute("resource-compute-output-mismatch", differences=output_differences,
                         next_action=("preserve the original compute receipt and output bytes; "
                                      "use a fresh completed run if they changed"),
                         retryable=False)
        fail_compute("resource-compute-evidence-changed", differences=differences,
                     next_action="restore the original completed compute-run evidence before replaying",
                     retryable=False)

    provenance = execution.get("provenance") or {}
    if (provenance.get("route_id") != route.get("route_id")
            or provenance.get("route_hash") != route.get("route_hash")
            or provenance.get("route_file") != str(Path(route_file).resolve(strict=True))
            or Path(provenance.get("registry", "")).resolve(strict=False) != registry):
        fail_compute("resource-compute-route-mismatch", differences=[
            {"field": "provenance.route_id", "expected": route.get("route_id"),
             "actual": provenance.get("route_id")},
            {"field": "provenance.route_hash", "expected": route.get("route_hash"),
             "actual": provenance.get("route_hash")},
            {"field": "provenance.route_file", "expected": str(Path(route_file).resolve(strict=True)),
             "actual": provenance.get("route_file")},
        ], next_action="select a completed run recorded for this exact route")

    identity = compute_execution_identity({"execution": execution})
    if identity is None:
        required_evidence = ("run_id", "meta_sha256", "exit_sha256", "receipt_sha256",
                             "outputs_sha256", "contract_sha256")
        missing = [{"field": "execution." + key, "expected": "non-empty recorded evidence",
                    "actual": execution.get(key)}
                   for key in required_evidence
                   if not isinstance(execution.get(key), str) or not execution[key]]
        fail_compute("resource-compute-output-missing" if any(
            item["field"] == "execution.outputs_sha256" for item in missing
        ) else "resource-compute-observation-unavailable",
            differences=missing or [{"field": "execution.kind", "expected": "compute-hosts",
                                     "actual": execution.get("kind")}],
            next_action="verify the persisted compute-run evidence before adopting it")
    if (execution.get("run_id") != compute_run_id
            or not isinstance(execution.get("run_root"), str) or not execution["run_root"]):
        fail_compute("resource-compute-observation-unavailable", differences=[
            {"field": "execution.locator", "expected": {
                "run_id": compute_run_id, "run_root": "canonical configured run root"},
             "actual": {"run_id": execution.get("run_id"),
                        "run_root": execution.get("run_root")}}],
            next_action="restore the configured compute-run locator before adopting it")

    route_path = str(Path(route_file).resolve(strict=True))
    node_id = provenance.get("node")

    def replay_evidence_differences(row):
        stored = row.get("execution") if isinstance(row, dict) else None
        if not isinstance(stored, dict) or stored.get("kind") != "compute-hosts":
            return [{"field": "execution", "expected": "stored compute-hosts evidence",
                     "actual": stored}]
        found = []
        # Keep the old identity string stable for existing markers; compare every
        # immutable receipt and locator field independently before reusing a row.
        for key in ("outputs_sha256", "run_id", "run_root", "host", "remote_host",
                    "meta_sha256", "exit_sha256", "receipt_sha256", "contract_sha256",
                    "contract", "identity", "provenance", "outputs", "receipt_state",
                    "started_at", "ended_at"):
            if stored.get(key) != execution.get(key):
                found.append({"field": "execution." + key,
                              "expected": stored.get(key), "actual": execution.get(key)})
        if row.get("execution", {}).get("identity_key") != identity:
            found.append({"field": "execution.identity_key", "expected": identity,
                          "actual": row.get("execution", {}).get("identity_key")})
        for field, expected, actual in (
                ("route", route_path, row.get("route")),
                ("node", node_id, row.get("node")),
                ("jobs", str(jobs), row.get("jobs")),
                ("compute_run_id", compute_run_id, row.get("compute_run_id"))):
            if expected != actual:
                found.append({"field": field, "expected": expected, "actual": actual})
        return found

    def adopted_locator_rows(rows):
        return [row for row in rows
                if isinstance(row, dict)
                and isinstance(row.get("execution"), dict)
                and row["execution"].get("kind") == "compute-hosts"
                and row["execution"].get("run_root") == execution.get("run_root")
                and row["execution"].get("run_id") == compute_run_id
                and row.get("route") == route_path and row.get("node") == node_id]

    # Discover an already-adopted run by its canonical execution locator before
    # identity lookup or retry ordinal selection. A changed receipt must never
    # become a new __aN row merely because it no longer matches the old identity.
    registry_snapshot = json.loads(registry.read_text(encoding="utf-8"))
    locator_rows = adopted_locator_rows((registry_snapshot.get("runs") or {}).values())
    if len(locator_rows) > 1:
        fail_compute("resource-compute-binding-conflict", differences=[
            {"field": "compute_run_locator.matches", "expected": 1,
             "actual": len(locator_rows)}],
            next_action="resolve duplicate adopted rows through the existing resource owner")
    if locator_rows:
        differences = replay_evidence_differences(locator_rows[0])
        if differences:
            fail_compute("resource-compute-evidence-changed", differences=differences,
                         next_action=("preserve the published row and verify its original evidence; "
                                      "use a fresh compute run through the existing route if outputs changed"),
                         retryable=False)

    ledger = sup.ledger_for(route, str(jobs))
    armed_rows = sup.read_armed(ledger)
    eligible = []
    differences = []
    refusal_reason = "resource-compute-contract-unavailable"
    for node_id, armed in armed_rows.items():
        if (armed.get("predecessor_kind") != "resource"
                or armed.get("resource_registry") != str(registry)
                or armed.get("route_id") != route.get("route_id")
                or armed.get("route_hash") != route.get("route_hash")
                or armed.get("jobs") != str(jobs)):
            continue
        node = WS.route_node(route, node_id)
        if not isinstance(node, dict) or node.get("kind") != "resource-runner":
            continue
        original = json.loads(registry.read_text(encoding="utf-8")).get("runs", {}).get(
            armed.get("predecessor_id"))
        parent = provenance.get("contract") or {}
        candidate_bindings = [
            ("resource_policy", "supervised-owner",
             original.get("resource_policy") if isinstance(original, dict) else None),
            ("resource.route", str(Path(route_file).resolve(strict=True)),
             original.get("route") if isinstance(original, dict) else None),
            ("resource.node", node_id, original.get("node") if isinstance(original, dict) else None),
            ("resource.jobs", str(jobs), original.get("jobs") if isinstance(original, dict) else None),
            ("provenance.run_id", original.get("run_id") if isinstance(original, dict) else None,
             provenance.get("run_id")),
            ("provenance.node", node_id, provenance.get("node")),
            ("provenance.parent_attempt_id",
             original.get("parent_attempt_id") if isinstance(original, dict) else None,
             provenance.get("parent_attempt_id")),
            ("contract.run_id", original.get("run_id") if isinstance(original, dict) else None,
             parent.get("run_id")),
            ("contract.route_hash", route.get("route_hash"), parent.get("route_hash")),
        ]
        candidate_differences = [{"field": field, "expected": expected, "actual": actual}
                                 for field, expected, actual in candidate_bindings
                                 if expected != actual]
        if candidate_differences:
            differences.extend(candidate_differences)
            refusal_reason = "resource-compute-binding-conflict"
            continue
        if (parent.get("artifact_base") != armed.get("artifact_base")
                or parent.get("declared_outputs") != list(node.get("outputs") or [])):
            differences.append({"field": "declared_outputs", "expected": {
                                    "artifact_base": armed.get("artifact_base"),
                                    "outputs": list(node.get("outputs") or [])},
                                "actual": {"artifact_base": parent.get("artifact_base"),
                                           "outputs": parent.get("declared_outputs")}})
            continue
        sealed = parent.get("sealed") or {}
        baseline_differences = []
        for key in ("command", "cwd", "config_ref", "config_sha256", "source_commit",
                    "source_dirty", "source_git_state", "config_layout", "launch_request"):
            if sealed.get(key) != original.get(key):
                baseline_differences.append({"field": "resource." + key,
                                             "expected": original.get(key), "actual": sealed.get(key)})
        if baseline_differences:
            differences.extend(baseline_differences)
            continue
        launch_request = original.get("launch_request") or {}
        input_rows = parent.get("inputs")
        expected_inputs = {name: (launch_request.get(name), launch_request.get(name + "_sha256"))
                           for name in ("smoke_attestation", "config_manifest")
                           if launch_request.get(name) is not None
                           or launch_request.get(name + "_sha256") is not None}
        input_map = {item.get("name"): item for item in input_rows or [] if isinstance(item, dict)}
        input_diff = []
        if not launch_request.get("smoke_attestation") or not launch_request.get("smoke_attestation_sha256"):
            input_diff.append({"field": "inputs.smoke_attestation", "expected": "sealed hash-bound input",
                               "actual": None})
        for name, (path, expected_sha) in expected_inputs.items():
            item = input_map.get(name) or {}
            actual_sha = None
            try:
                input_path = Path(path)
                info = input_path.lstat()
                raw = input_path.read_bytes()
                actual_sha = hashlib.sha256(raw).hexdigest()
                if (input_path.is_symlink() or not stat.S_ISREG(info.st_mode)
                        or actual_sha != expected_sha or item.get("verified") is not True
                        or item.get("path") != path or item.get("sha256") != expected_sha):
                    raise ValueError("sealed input changed")
                tool = Path(__file__).parents[1] / "tools" / (
                    "smoke-attestation.py" if name == "smoke_attestation" else "lab-config-provenance.py")
                verify_args = ([sys.executable, str(tool), "verify", "--attestation", path]
                               if name == "smoke_attestation" else
                               [sys.executable, str(tool), "verify", "--manifest", path])
                verify_launch_input(verify_args, stdout=subprocess.DEVNULL)
            except (OSError, TypeError, ValueError, subprocess.CalledProcessError):
                input_diff.append({"field": "inputs." + name + ".sha256",
                                   "expected": expected_sha, "actual": actual_sha})
        if input_diff:
            differences.extend(input_diff)
            continue
        contract = execution.get("contract") or {}
        launch = original.get("command")
        expected_contract = sealed_compute_command(launch)
        if expected_contract is None:
            differences.append({"field": "compute-hosts.command", "expected": "explicit --cwd, --env and -- command",
                                "actual": launch})
            continue
        current_differences = [{"field": "compute-hosts." + key, "expected": value,
                                "actual": contract.get(key)}
                               for key, value in expected_contract.items()
                               if contract.get(key) != value]
        if current_differences:
            differences.extend(current_differences)
            continue
        try:
            if armed.get("resource_binding") != OWNER_RESOURCE.resource_body_digest(original):
                differences.append({"field": "resource_binding",
                                    "expected": OWNER_RESOURCE.resource_body_digest(original),
                                    "actual": armed.get("resource_binding")})
                refusal_reason = "resource-compute-binding-conflict"
                continue
            if original.get("status") != "failed" or original.get("exit_code") in (None, 0):
                continue
            from resource_run_registry import (classify_identity, compute_execution_observation,
                                               resource_process_group_quiescent)
            original_execution = original.get("execution") if isinstance(original, dict) else None
            if isinstance(original_execution, dict) and original_execution.get("kind") == "compute-hosts":
                original_observation = compute_execution_observation(original)
                liveness = ("exited" if original_observation.get("state") == "quiescent" else "stale")
                original_quiescent = original_observation.get("state") == "quiescent"
            else:
                liveness, _identity, _reason = classify_identity(original)
                original_quiescent = (liveness == "exited"
                                       and resource_process_group_quiescent(original))
            if (liveness != "exited"
                    or read_sentinel(original.get("sentinel")) != original.get("exit_code")
                    or not original_quiescent):
                continue
        except (OSError, ValueError, KeyError, TypeError):
            continue
        eligible.append((node_id, armed, node, original))
    if not eligible:
        # After the adopted row has been armed, the current predecessor is no
        # longer the original failed row named by compute provenance. Find the
        # already-published identity by its exact route/node binding so replay
        # can converge before treating that successful row as a new baseline.
        replay_node_id = node_id
        replay_node = WS.route_node(route, replay_node_id) if isinstance(replay_node_id, str) else None
        replay_armed = armed_rows.get(replay_node_id) if isinstance(replay_node_id, str) else None
        replay_rows = [row for row in (json.loads(registry.read_text(encoding="utf-8")).get("runs") or {}).values()
                       if isinstance(row, dict)
                       and identity is not None
                       and (row.get("execution") or {}).get("identity_key") == compute_execution_identity(
                           {"execution": execution})
                       and row.get("route") == str(Path(route_file).resolve(strict=True))
                       and row.get("node") == replay_node_id and row.get("jobs") == str(jobs)
                       and row.get("compute_run_id") == compute_run_id
                       and (row.get("execution") or {}).get("run_root") == execution.get("run_root")
                       and replay_armed is not None
                       and replay_armed.get("predecessor_id") in {
                           row.get("run_id"), (provenance.get("contract") or {}).get("run_id")}
                       and row.get("status") == "succeeded"]
        if len(replay_rows) == 1 and isinstance(replay_node, dict):
            eligible.append((replay_node_id, replay_armed, replay_node, replay_rows[0]))
    if len(eligible) != 1:
        if eligible:
            fail_compute("resource-compute-baseline-ambiguous")
        if differences:
            fail_compute(refusal_reason if refusal_reason == "resource-compute-binding-conflict"
                         else "resource-compute-contract-mismatch", differences=differences,
                         next_action="run the existing smoke/config flow for the changed contract")
        fail_compute(refusal_reason, differences=[{"field": "resource-candidate",
                    "expected": "one exactly bound failed resource row", "actual": "no eligible row"}],
                     next_action="repair the existing route/resource binding before retrying")
    node_id, armed, node, original = eligible[0]

    # A run's declared science outputs must already exist at their route-owned
    # paths. The compute-hosts matcher expands concrete glob members while
    # retaining abstract handoff outputs as non-path declarations.
    artifact_base = Path(armed.get("artifact_base") or "")
    declared = list(node.get("outputs") or [])
    output_rows = execution.get("outputs")
    output_check = COMPUTE.validate_compute_outputs(artifact_base, declared, output_rows)
    if not output_check.get("ok"):
        reason = output_check.get("reason") or "compute-run-output-missing"
        token = compute_output_refusal(reason)
        fail_compute(token, differences=output_check.get("differences") or [{
                         "field": "outputs", "expected": "sealed declared outputs", "actual": reason}],
                     next_action=output_check.get("next_action")
                     or "verify the candidate run's declared output files",
                     retryable=output_check.get("retryable", False))

    try:
        owner_wait = OWNER_RESOURCE.start_binding(route, route_file, args, os.environ)
    except Exception as error:
        reason = str(error)
        token = ("resource-compute-route-closed" if "cancel" in reason or "close" in reason
                 else "resource-compute-owner-unavailable")
        fail_compute(token, differences=[{"field": "owner", "expected": "current live route owner",
                                          "actual": reason[:256]}])
    if not owner_wait:
        fail_compute("resource-compute-owner-unavailable")

    data = json.loads(registry.read_text(encoding="utf-8"))
    runs = data.get("runs") or {}
    # Recheck the locator against the latest registry snapshot as well. A
    # concurrent publisher may have written the same run after the earlier
    # observation; a different evidence digest must not fall through to __aN.
    locator_rows = adopted_locator_rows(runs.values())
    if len(locator_rows) > 1:
        fail_compute("resource-compute-binding-conflict", differences=[
            {"field": "compute_run_locator.matches", "expected": 1,
             "actual": len(locator_rows)}],
            next_action="resolve duplicate adopted rows through the existing resource owner")
    if locator_rows:
        differences = replay_evidence_differences(locator_rows[0])
        if differences:
            fail_compute("resource-compute-evidence-changed", differences=differences,
                         next_action=("preserve the published row and verify its original evidence; "
                                      "use a fresh compute run through the existing route if outputs changed"),
                         retryable=False)
    # Stable identity lookup precedes retry ordinal selection, so retry after
    # publication/arm interruption converges on the same row.
    existing = [row for row in runs.values() if identity is not None and isinstance(row, dict)
                and (row.get("execution") or {}).get("identity_key") == identity
                and row.get("route") == route_path and row.get("node") == node_id
                and row.get("jobs") == str(jobs) and row.get("compute_run_id") == compute_run_id
                and (row.get("execution") or {}).get("run_root") == execution.get("run_root")]
    if len(existing) > 1:
        fail_compute("resource-compute-binding-conflict", differences=[
            {"field": "compute_execution_identity.matches", "expected": 1, "actual": len(existing)}],
            next_action="resolve duplicate adopted rows through the existing resource owner")
    def arm_adopted(adopted_row):
        current_armed = sup.read_armed(ledger).get(node_id)
        if current_armed and current_armed.get("predecessor_id") == adopted_row.get("run_id"):
            return
        artifacts = __import__("artifact_producer").prepare_route_artifact_env(
            route_file, start=True, jobs=jobs)
        command = [sys.executable, str(Path(__file__).with_name("workflow-supervisor.py")), "arm",
                   "--route", str(route_file), "--node", node_id, "--predecessor-kind", "resource",
                   "--predecessor-id", adopted_row["run_id"], "--resource-registry", str(registry),
                   "--jobs", str(jobs), "--artifact-base", artifacts["AGENT_ARTIFACT_OUTPUT_DIR"],
                   "--successor-external", "--successor-cwd", str(adopted_row.get("cwd") or route.get("cwd")),
                   "--successor-log", str(ledger.root / "resource" / "verification-start.log")]
        try:
            subprocess.run(command, check=True, env={**os.environ, "AGENT_DISPATCH_JOBS": str(jobs)},
                           stdout=subprocess.DEVNULL, timeout=30)
        except (OSError, subprocess.SubprocessError) as error:
            fail_compute("resource-compute-binding-conflict",
                         differences=[{"field": "resource-arm", "expected": adopted_row["run_id"],
                                       "actual": type(error).__name__}],
                         next_action="repeat the same compute-run start to finish the published arm")
    if existing:
        adopted = existing[0]
        differences = replay_evidence_differences(adopted)
        if differences:
            fail_compute("resource-compute-evidence-changed", differences=differences,
                         next_action=("preserve the published row and verify its original evidence; "
                                      "use a fresh compute run through the existing route if outputs changed"),
                         retryable=False)
        require_current_compute_snapshot()
        if adopted.get("parent_attempt_id") != owner_wait["parent_attempt_id"]:
            fail_compute("resource-compute-binding-conflict", differences=[
                {"field": "parent_attempt_id", "expected": owner_wait["parent_attempt_id"],
                 "actual": adopted.get("parent_attempt_id")}],
                next_action="resume through the owner route bound to this compute run")
        arm_adopted(adopted)
        return {**adopted, "replayed": True, "payload_spawned": False,
                "compute_run_id": compute_run_id, "next_action": "start run-verify through its existing owner route"}

    suffix = RETRY.fullmatch(str(armed["predecessor_id"]))
    base = str(armed["predecessor_id"])[:suffix.start()] if suffix else str(armed["predecessor_id"])
    ordinal = int(suffix["n"]) + 1 if suffix else 1
    while f"{base}__a{ordinal}" in runs:
        ordinal += 1
    adopted_id = f"{base}__a{ordinal}"
    run_dir = Path(execution["run_root"]) / compute_run_id
    adopted = {key: value for key, value in original.items()
               if key not in {"pid", "starttime", "command_hash", "pid_namespace", "process_group",
                              "launch_argv", "supervision", "gpu_lease", "gpus", "watch_pid"}}
    adopted.update({
        "run_id": adopted_id, "route": str(Path(route_file).resolve(strict=True)),
        "node": node_id, "jobs": str(jobs), "parent_attempt_id": owner_wait["parent_attempt_id"],
        "owner_wait": owner_wait, "resource_policy": "supervised-owner",
        "status": "succeeded", "workflow_state": "STAGE_SUCCEEDED", "exit_code": 0,
        "failure_class": None, "started_at": execution.get("started_at"),
        "ended_at": execution.get("ended_at"), "log": str(run_dir / "log"),
        "sentinel": str(run_dir / "exit_code"), "execution": {**execution, "identity_key": identity},
        "compute_run_id": compute_run_id,
    })
    adopted["resource_binding"] = OWNER_RESOURCE.resource_body_digest(adopted)
    if OWNER_RESOURCE.resource_evidence_paths_conflict(original, adopted):
        fail_compute("resource-compute-binding-conflict", differences=[
            {"field": "resource.evidence_paths", "expected": "no shared evidence path or inode",
             "actual": {"existing_run": original.get("run_id"), "adopted_run": adopted_id,
                        "existing": [original.get(key) for key in ("log", "sentinel", "progress_file")],
                        "candidate": [adopted.get(key) for key in ("log", "sentinel", "progress_file")]}}],
            next_action="repair the conflicting resource evidence paths before retrying")
    with ledger.lock():
        if sup.resource_continuation_cancelled(route, ledger):
            fail_compute("resource-compute-route-closed", differences=[
                {"field": "route.state", "expected": "open", "actual": "closed or cancelled"}],
                next_action="continue only after the route owner opens a new authorized route")
        current_armed = sup.read_armed(ledger).get(node_id)
        if current_armed != armed:
            fail_compute("resource-compute-binding-conflict", differences=[
                {"field": "armed_row", "expected": hashlib.sha256(json.dumps(
                    armed, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                 "actual": hashlib.sha256(json.dumps(current_armed, sort_keys=True,
                    separators=(",", ":")).encode()).hexdigest() if current_armed is not None else None}],
                next_action="re-read the current owner route state before retrying the same compute run")
        state = ledger.state()
        stage = state.get("nodes", {}).get(node_id, {})
        failure_states = WS.vocabulary()["failure_states"]
        invalid_states = []
        if stage.get("state") not in {"RUNNING", "FAILED_RETRYABLE"}:
            invalid_states.append({"field": f"nodes.{node_id}.state",
                                   "expected": ["RUNNING", "FAILED_RETRYABLE"],
                                   "actual": stage.get("state")})
        if state.get("workflow_state") not in {
                "RUNNING", "STAGE_SUCCEEDED", "NEXT_REGISTERED", "NEXT_RUNNING", "FAILED_RETRYABLE"}:
            invalid_states.append({"field": "workflow_state",
                                   "expected": ["RUNNING", "STAGE_SUCCEEDED", "NEXT_REGISTERED",
                                                "NEXT_RUNNING", "FAILED_RETRYABLE"],
                                   "actual": state.get("workflow_state")})
        failed_nodes = [name for name, value in state.get("nodes", {}).items()
                        if name != node_id and value.get("state") in failure_states]
        if failed_nodes:
            invalid_states.append({"field": "other_failed_nodes", "expected": [], "actual": failed_nodes})
        claims = [claim for claim in ledger.claims().values() if claim.get("predecessor") == node_id]
        if claims:
            invalid_states.append({"field": "successor_claims", "expected": [],
                                   "actual": [claim.get("successor") for claim in claims]})
        if invalid_states:
            fail_compute("resource-compute-binding-conflict", differences=invalid_states,
                         next_action="resume only after the current route stage state is consistent")
        if stage.get("state") == "FAILED_RETRYABLE":
            previous = stage.get("evidence") or {}
            if (previous.get("resource_sha256") != RESOURCE_RESUME.row_digest(original)
                    or previous.get("succeeded") is not False
                    or previous.get("exit_code") != original.get("exit_code")):
                fail_compute("resource-compute-binding-conflict", differences=[
                    {"field": "stage.evidence.resource_sha256",
                     "expected": RESOURCE_RESUME.row_digest(original),
                     "actual": previous.get("resource_sha256")},
                    {"field": "stage.evidence.succeeded", "expected": False,
                     "actual": previous.get("succeeded")},
                    {"field": "stage.evidence.exit_code", "expected": original.get("exit_code"),
                     "actual": previous.get("exit_code")}],
                    next_action="restore the existing failed-resource evidence before retrying")
        if state.get("workflow_state") == "FAILED_RETRYABLE":
            workflow_failure = next((entry for entry in reversed(ledger.journal())
                                     if entry.get("workflow_state")), {})
            if (state.get("nodes", {}).get(node_id, {}).get("state") != "FAILED_RETRYABLE"
                    and (workflow_failure.get("evidence") or {}).get("node") != node_id):
                fail_compute("resource-compute-binding-conflict", differences=[
                    {"field": "workflow_failure.evidence.node", "expected": node_id,
                     "actual": (workflow_failure.get("evidence") or {}).get("node")}],
                    next_action="resume the route from its recorded failed stage")
        for _other_armed, other_row in sup.resource_predecessors(ledger, node_id):
            if (other_row.get("run_id") != original.get("run_id")
                    and OWNER_RESOURCE.resource_evidence_paths_conflict(other_row, adopted)):
                fail_compute("resource-compute-binding-conflict", differences=[
                    {"field": "resource.evidence_paths", "expected": "no shared evidence path or inode",
                     "actual": {"conflicting_run": other_row.get("run_id"),
                                "candidate_run": adopted_id,
                                "candidate_paths": [adopted.get(key) for key in
                                                    ("log", "sentinel", "progress_file")]}}],
                    next_action="repair the conflicting resource evidence paths before retrying")
        with owner_input_lock(jobs, owner_wait["parent_attempt_id"]):
            parent_row, target = owner_input_target(jobs, owner_wait["parent_attempt_id"])
            if parent_row.status not in {"open", "running"} or not target:
                fail_compute("resource-compute-owner-input-unavailable", differences=[
                    {"field": "owner_input.status", "expected": ["open", "running"],
                     "actual": parent_row.status},
                    {"field": "owner_input.target", "expected": "available", "actual": bool(target)}],
                    next_action="resume the current owner input, then repeat the same compute-run start")
            if sup.resource_continuation_cancelled(route, ledger):
                fail_compute("resource-compute-route-closed", differences=[
                    {"field": "route.state", "expected": "open", "actual": "closed or cancelled"}],
                    next_action="continue only after the route owner opens a new authorized route")
            require_current_compute_snapshot()
            def publish(document):
                current = document["runs"].get(original["run_id"])
                if current != original or adopted_id in document["runs"]:
                    raise ValueError(json.dumps({"field": "resource_registry.publication",
                        "expected": {"original_row_sha256": hashlib.sha256(json.dumps(
                            original, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                            "adopted_id_absent": True},
                        "actual": {"original_row_sha256": hashlib.sha256(json.dumps(
                            current, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                            if current is not None else None,
                            "adopted_id_sha256": hashlib.sha256(json.dumps(
                                document["runs"][adopted_id], sort_keys=True,
                                separators=(",", ":")).encode()).hexdigest()
                                if adopted_id in document["runs"] else None}}))
                document["runs"][adopted_id] = adopted
            try:
                locked_update(registry, publish)
            except ValueError as error:
                try:
                    detail = json.loads(str(error))
                except ValueError:
                    detail = {"field": "resource_registry.publication", "expected": "unchanged row and unused id",
                              "actual": str(error)}
                fail_compute("resource-compute-binding-conflict", differences=[detail],
                             next_action="re-read the resource registry and retry only if the original binding remains current")
    arm_adopted(adopted)
    print(json.dumps({**adopted, "replayed": False, "payload_spawned": False,
                      "compute_run_id": compute_run_id,
                      "next_action": "start run-verify through its existing owner route"}, sort_keys=True))


def publish_verified_run(registry, run_id, expected, published):
    """Only the exact reserved row can release this payload; a foreign row survives."""
    def apply(data):
        if data["runs"].get(run_id) != expected:
            raise ValueError("resource-reservation-changed")
        data["runs"][run_id] = published
    locked_update(registry, apply)


def launch_error(error):
    diagnostic = getattr(error, "stderr", None) or getattr(error, "resource_message", None)
    if isinstance(diagnostic, bytes):
        diagnostic = diagnostic.decode("utf-8", errors="replace")
    return {"type": type(error).__name__, "message": str(diagnostic or error).strip()[:2048]}


def verify_launch_input(command, **kwargs):
    """Keep a verifier's actual diagnostic when returning its refusal to the owner."""
    try:
        result = subprocess.run(command, check=True, stderr=subprocess.PIPE, text=True, **kwargs)
    except subprocess.CalledProcessError as error:
        if error.stderr:
            print(error.stderr, file=sys.stderr, end="")
        raise
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")


def settle_queued_validation_failure(controller, error):
    """Only this leased launcher's unchanged pre-claim request proves no payload ran."""
    expected = controller.expected
    if (isinstance(error, LaunchDeferred) or expected.get("status") != "launching"
            or expected.get("launch_state") != "queued"
            or any(expected.get(k) is not None for k in
                   ("pid", "starttime", "command_hash", "process_group", "launch_controller"))):
        return
    import dispatch_resource_wait as OWNER_RESOURCE
    sup = OWNER_RESOURCE.supervisor()
    route = sup.load_route(expected["route"])
    ledger = sup.ledger_for(route, expected["jobs"])
    with ledger.lock():
        current = json.loads(Path(controller.registry).read_text())["runs"].get(expected["run_id"])
        if current != expected:
            return
        armed = sup.read_armed(ledger).get(expected["node"])
        if (not armed or armed.get("predecessor_id") != expected["run_id"]
                or armed.get("resource_registry") != str(Path(controller.registry).resolve())
                or armed.get("resource_binding") != OWNER_RESOURCE.resource_body_digest(expected)
                or sup.resource_continuation_cancelled(route, ledger)):
            return
        failed = {**expected, "status": "failed", "workflow_state": "FAILED_RETRYABLE",
            "failure_class": "resource-launch-incomplete", "launch_state": "not-started",
            "launch_controller": controller.identity, "ended_at": time.time(), "exit_code": None,
            "launch_error": launch_error(error)}
        if not resource_never_started(failed):
            return
        with controller.guard():
            # The existing reservation lock rejects a claim or foreign successor.
            publish_verified_run(controller.registry, expected["run_id"], expected, failed)
        sup._evaluate(route, ledger, armed, [])
        return failed


def main(argv=None, *, controller=None):
    try:
        return _main(argv, controller=controller)
    except (Exception, SystemExit) as error:
        if controller is not None and (not isinstance(error, SystemExit) or error.code == 65):
            failed = settle_queued_validation_failure(controller, error)
            decision = route_authority.decide_continuation(action="resource-failure", facts={
                "not_started": failed is not None and resource_never_started(failed),
                "deferred": isinstance(error, LaunchDeferred)})
            if decision.allowed:
                print(json.dumps({**failed, "payload_spawned": False, "workflow_complete": False}))
                return
        raise


def _main(argv=None, *, controller=None):
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] == "start":
        import artifact_context
        context = artifact_context.resolve_context(environ=os.environ)
        if context.route and context.node:
            def option_value(name):
                try:
                    return raw_argv[raw_argv.index(name) + 1]
                except (ValueError, IndexError):
                    return next((part[len(name) + 1:] for part in raw_argv
                                 if part.startswith(name + "=")), None)

            divider = raw_argv.index("--") if "--" in raw_argv else len(raw_argv)
            if option_value("--registry") is None:
                if not context.output_dir:
                    fail("artifact-cycle-unbound")
                from resource_run_registry import route_registry_path
                raw_argv[0:0] = ["--registry", str(route_registry_path(context.output_dir))]
                divider += 2
            defaults = {}
            if option_value("--cwd") is None:
                defaults["--cwd"] = str(context.source_worktree or "")
            elif context.source_worktree and Path(option_value("--cwd")).expanduser().resolve(strict=False) != context.source_worktree:
                fail("source-worktree-route-mismatch")
            route_path = (context.artifact_root / ".runtime" / "routes"
                          / f"{context.route['route_id']}.json")
            if option_value("--route") is None:
                defaults["--route"] = str(route_path)
            else:
                import artifact_producer
                supplied = artifact_producer.resolve_route_argument(context.artifact_root, Path(option_value("--route")))
                if Path(supplied).resolve(strict=False) != route_path.resolve(strict=False):
                    fail("route-binding-mismatch")
            if option_value("--node") is None:
                defaults["--node"] = context.node_id or ""
            elif option_value("--node") != context.node_id:
                fail("route-node-mismatch")
            run_id = option_value("--run-id")
            if run_id and option_value("--log") is None:
                binding = context.resolve_output("resource-log", leaf=f"logs/{run_id}.log")
                defaults["--log"] = str(binding.path)
            elif option_value("--log") is not None:
                context.resolve_output("resource-log", explicit=option_value("--log"))
            for flag, value in defaults.items():
                if value:
                    raw_argv[divider:divider] = [flag, value]
                    divider += 2
    p=argparse.ArgumentParser(); p.add_argument("--registry"); s=p.add_subparsers(dest="cmd",required=True)
    a=s.add_parser("start"); a.add_argument("--run-id"); a.add_argument("--cwd"); a.add_argument("--log"); a.add_argument("--route",required=True); a.add_argument("--node"); a.add_argument("--compute-run"); a.add_argument("--smoke-attestation"); a.add_argument("--config-manifest")
    a.add_argument("--parent-attempt-id",help="registered headless attempt that owns this resource child")
    a.add_argument("--jobs", help="canonical dispatch registry for the existing continuation")
    a.add_argument("--share", action="store_true", help="Allow intentional GPU sharing")
    a.add_argument("command",nargs=argparse.REMAINDER)
    for name in ("status","stop","tail","reap"):
        x=s.add_parser(name); x.add_argument("--run-id",required=True)
    observation_cmd=s.add_parser("observation", help="read a run's stable public observation record")
    observation_cmd.add_argument("--run-id",required=True)
    observation_cmd.add_argument("--json",action="store_true",required=True)
    s.add_parser("list")
    index_cmd=s.add_parser("index"); index_cmd.add_argument("--registry",required=True)
    args=p.parse_args(raw_argv)
    if args.cmd=="index":
        registry=Path(args.registry).resolve(strict=True)
        indexed=register_registry(registry)
        print(json.dumps({"registry":str(registry),**indexed},sort_keys=True))
        return
    if not args.registry: fail("--registry is required")
    registry=Path(args.registry).resolve()
    if args.cmd=="start":
        command=args.command[1:] if args.command[:1]==["--"] else args.command
        route_file=Path(args.route)
        if route_file.is_symlink():
            fail("route-file-unsafe")
        route_file=route_file.resolve(strict=True)
        route=json.loads(route_file.read_text())
        if args.compute_run:
            if not SAFE_RUN_ID.fullmatch(args.compute_run):
                fail("resource-compute-run-unavailable:invalid-id")
            if args.cwd is None:
                args.cwd = route.get("cwd")
        elif not args.run_id or not args.cwd or not args.log or not args.node:
            fail("--run-id, --cwd, --log, and --node are required for normal start")
        if not args.compute_run and not command: fail("command required")
        if not args.cwd:
            fail("--cwd is required")
        cwd=Path(args.cwd).resolve(strict=True)
        if route.get("capability") not in {"autopilot-code", "autopilot-lab"}:
            fail("route-capability-not-accepted")
        artifact_root=Path(str(route.get("artifact_root", ""))).resolve()
        if not route_file.is_relative_to(artifact_root):
            fail("route-file-outside-artifact-root")
        verify_launch_input([
            sys.executable, str(Path(__file__).with_name("capability-route.py")),
            "verify", "--route", str(route_file), "--cwd", str(cwd),
        ], stdout=subprocess.DEVNULL)
        if args.compute_run:
            adopt_compute_run(registry, args, route, route_file, args.compute_run)
            return
        node=next((n for n in route["nodes"] if isinstance(n,dict) and n.get("id")==args.node),None)
        if not node or node.get("kind")!="resource-runner" or node.get("resource_transport")!="detached-process":
            fail("route node is not detached resource-runner")
        resume = RESOURCE_RESUME.route_selected(route) and node.get("resource_policy") == "verified-resume"
        if resume and not SAFE_RUN_ID.fullmatch(args.run_id): fail("invalid --run-id")
        if not args.smoke_attestation and not resume: fail("hash-bound smoke attestation required")
        if args.smoke_attestation:
            verify_launch_input([sys.executable,str(Path(__file__).parents[1]/"tools/smoke-attestation.py"),"verify","--attestation",args.smoke_attestation])
        provenance = {}
        if args.config_manifest:
            manifest = json.loads(Path(args.config_manifest).read_text())
            verify_tool = Path(__file__).parents[1] / "tools" / "lab-config-provenance.py"
            verify_launch_input([sys.executable, str(verify_tool), "verify", "--manifest", args.config_manifest])
            if not SAFE_RUN_ID.match(args.run_id):
                fail("invalid --run-id")
            # --attempt suffix policy: "<manifest_run_id>__a<N>" retries the same
            # sealed manifest under a distinct registry key; anything else must
            # match the manifest's run_id exactly. The registry row's run_id is
            # always args.run_id (the registry key), never the manifest's.
            # Order is load-bearing (A9): the exact match must be checked before
            # the regex, since a computed run id's 12-hex hash tail can
            # coincidentally read as "a" + digits and get mis-split otherwise.
            manifest_run_id = manifest["run_id"]
            if args.run_id != manifest_run_id:
                m = RETRY.fullmatch(args.run_id)
                if not (m and m["base"] == manifest_run_id):
                    fail("run id does not match sealed manifest")
            attestation = json.loads(Path(args.smoke_attestation).read_text()) if args.smoke_attestation else None
            if attestation is not None and attestation.get("config_sha256") != manifest.get("snapshot_sha256"):
                fail("config provenance does not match smoke attestation")
            if attestation is not None and attestation.get("config_source_sha256") != manifest.get("source_sha256"):
                fail("config source provenance does not match smoke attestation")
            try:
                attested_source = Path(attestation.get("config_source_path", "")).resolve(strict=False) if attestation else None
            except (OSError, ValueError):
                attested_source = None
            if attestation is not None and attested_source != Path(manifest["source_path"]).resolve(strict=False):
                fail("config source path does not match smoke attestation")
            provenance = {"config_ref": manifest["config_ref"], "config_sha256": manifest["snapshot_sha256"],
                          "source_commit": manifest["source_commit"], "source_dirty": manifest["source_dirty"],
                          "source_git_state": manifest.get("source_git_state", "unknown-no-git"),
                          "config_layout": manifest.get("config_layout", "unknown")}
        log=Path(args.log).resolve()
        sentinel=Path(str(log)+".exit")
        launcher_session = None
        try:
            import session_identity
            identity = session_identity.identity(os.environ)
            if identity.known and identity.session_id:
                launcher_session = {"harness": identity.harness, "id": identity.session_id}
        except Exception:
            pass
        jobs_path = args.jobs or os.environ.get("AGENT_DISPATCH_JOBS") or None
        placeholder={"run_id":args.run_id,"cwd":str(cwd),"log":str(log),"command":command,
                     **provenance,"route":args.route,"node":args.node,"status":"launching",
                     "route_id":route.get("route_id"), "artifact_root":str(artifact_root),
                     "campaign_id":os.environ.get("AGENT_ARTIFACT_CAMPAIGN_ID") or None,
                     "cycle_id":os.environ.get("AGENT_ARTIFACT_CYCLE_ID") or None,
                     "session":launcher_session,
                     "jobs":jobs_path,
                     "sentinel":str(sentinel),"progress_file":str(log)+".progress.json",
                     "parent_attempt_id":args.parent_attempt_id,
                     "workflow_state":"READY","started_at":time.time()}
        if args.share:
            placeholder["share"] = True
        owner_wait = None
        if not resume and (node.get("continuation") or {}).get("kind") == "supervised":
            import dispatch_resource_wait as OWNER_RESOURCE
            owner_wait = (controller.expected.get("owner_wait") if controller is not None else
                          OWNER_RESOURCE.start_binding(route, route_file, args, os.environ))
            if owner_wait:
                placeholder.update(parent_attempt_id=args.parent_attempt_id, owner_wait=owner_wait)
                placeholder["launch_request"] = launch_request(args)
                if owner_wait.get("launch_scope") == "codex-owner-controller":
                    if controller is not None and placeholder["launch_request"] != controller.expected.get("launch_request"):
                        fail("resource-launch-request-changed")
        if controller is not None and (not owner_wait or owner_wait.get("launch_scope") != "codex-owner-controller"):
            fail("resource-controller-scope-invalid")
        if resume or owner_wait:
            start_verified(registry, args, route, route_file, placeholder, controller=controller)
            return
        def reserve(data):
            if args.run_id in data["runs"]: raise ValueError("run id already exists")
            data["runs"][args.run_id]=placeholder
        locked_update(registry,reserve)
        try:
            register_registry(registry)
        except Exception:
            locked_update(registry,lambda data:data["runs"].pop(args.run_id,None))
            raise
        log.parent.mkdir(parents=True,exist_ok=True)
        with contextlib.suppress(OSError):
            sentinel.unlink()
        with contextlib.suppress(OSError):
            Path(str(sentinel)+".partial").unlink()
        lease_path, gpu_lease, gpu_env = None, None, {}
        try:
            lease_path, gpu_lease, gpu_env = gpu_leases.resource_admission(
                node, command, gpu_scoped=args.node in gpu_resource_nodes(route),
                share=args.share, jobs=args.jobs, run_id=args.run_id)
        except Exception:
            locked_update(registry,lambda data:data["runs"].pop(args.run_id,None))
            raise
        wait_read, release = os.pipe()
        launch_argv=["/bin/bash","-p","-c",'IFS= read -r launch <&"$AGENT_RESOURCE_LAUNCH_FD" || exit 125; '
                     + SENTINEL_SCRIPT,"resource-runner",*command]
        environment={**os.environ,**progress_environment(placeholder),"AGENT_RESOURCE_SENTINEL":str(sentinel),
                     "HEARTING_RESOURCE_RUN_ID": args.run_id, "HEARTING_RESOURCE_REGISTRY": str(registry.resolve()),
                     "HEARTING_GPU_LEASE_RELEASE": "", **gpu_env, "AGENT_RESOURCE_LAUNCH_FD": str(wait_read)}
        environment = gpu_payload_environment(environment, gpu_lease)
        out=open(log,"ab",buffering=0)
        try:
            proc=subprocess.Popen(launch_argv,cwd=cwd,env=environment,pass_fds=(wait_read,),stdout=out,stderr=subprocess.STDOUT,start_new_session=True)
        except Exception:
            out.close()
            os.close(wait_read); os.close(release)
            gpu_leases.release(lease_path, gpu_lease)
            locked_update(registry,lambda data:data["runs"].pop(args.run_id,None))
            raise
        os.close(wait_read)
        ident=launched_identity(proc)
        if not ident:
            os.close(release)
            proc.kill()
            gpu_leases.release(lease_path, gpu_lease)
            locked_update(registry,lambda data:data["runs"].pop(args.run_id,None))
            fail("could not establish process identity")
        run={**ident,"pid_namespace":os.readlink("/proc/self/ns/pid"),"run_id":args.run_id,"process_group":os.getpgid(proc.pid),"cwd":str(cwd),"log":str(log),"command":command,
             "launch_argv":launch_argv,"sentinel":str(sentinel),"progress_file":placeholder["progress_file"],
             "parent_attempt_id":args.parent_attempt_id,**provenance,
             "route":args.route,"node":args.node,"route_id":route.get("route_id"),
             "artifact_root":str(artifact_root),
             "campaign_id":placeholder.get("campaign_id"), "cycle_id":placeholder.get("cycle_id"),
             "session":launcher_session,
             "jobs":jobs_path,
             "status":"running","workflow_state":"RUNNING",
             "started_at":placeholder["started_at"]}
        def add(data):
            data["runs"][args.run_id]=run
        try:
            if gpu_lease:
                gpu_leases.bind(lease_path, gpu_lease, gpu_leases.identity(proc.pid))
                run.update(gpu_lease=gpu_lease, gpus=",".join(gpu_lease["gpus"]))
            if args.share:
                run["share"] = True
            locked_update(registry,add)
            os.write(release, b"start\n")
        except Exception:
            gpu_leases.release(lease_path, gpu_lease)
            def discard_own(data):
                if data["runs"].get(args.run_id) in (placeholder, run):
                    data["runs"].pop(args.run_id)
            locked_update(registry, discard_own)
            raise
        finally:
            os.close(release)
            out.close()
        print(json.dumps(run)); return
    data=json.loads(registry.read_text())
    if args.cmd=="list":
        rows=[]
        for run_id,row in sorted(data.get("runs",{}).items()):
            if isinstance(row,dict):
                row,_settled=settle(registry,run_id,row)
                liveness=classify_identity(row)[0]
                rows.append({**row,"liveness":liveness})
        print(json.dumps(rows,sort_keys=True)); return
    run=data["runs"].get(args.run_id)
    if not run: fail("unknown run id")
    if args.cmd=="observation":
        root=run.get("artifact_root") or os.environ.get("AGENT_ARTIFACT_ROOT")
        if root:
            import resource_progress
            record=resource_progress.read_public_run_record(root,args.run_id)
            if record is not None:
                print(json.dumps(record,ensure_ascii=False,sort_keys=True))
                return
        print(json.dumps({"run_id":args.run_id,"available":False},sort_keys=True))
        return
    if args.cmd=="tail": print(Path(run["log"]).read_text(errors="replace"),end=""); return
    if args.cmd in ("status","reap"):
        run,settled=settle(registry,args.run_id,run)
        if args.cmd=="reap":
            liveness=classify_identity(run)[0]
            print(json.dumps({**run,"liveness":liveness,"settled":settled},sort_keys=True)); return
    liveness,_,_=classify_identity(run)
    if args.cmd=="stop":
        if liveness!="working": fail("process identity is stale")
        try:
            pid=int(run["pid"]); group=int(run["process_group"])
            if os.getpgid(pid)!=group or group!=pid: fail("process group identity is stale")
        except (OSError,TypeError,ValueError,KeyError):
            fail("process group identity is stale")
        # Close the TOCTOU window as far as userspace allows: identity is
        # re-read immediately before signalling the exact group leader.
        if classify_identity(run)[0]!="working": fail("process identity changed before signal")
        try:
            if os.getpgid(pid)!=group: fail("process group changed before signal")
        except OSError:
            fail("process group changed before signal")
        if run.get("resource_policy") in {"verified-resume", "supervised-owner"}:
            def cancel(data):
                row = data["runs"][args.run_id]
                if any(row.get(k) != run.get(k) for k in ("pid", "starttime", "command_hash")):
                    raise ValueError("process identity changed before cancellation")
                row["cancel_requested"] = True
            locked_update(registry, cancel)
        os.killpg(group,signal.SIGTERM)
    status=run.get("status") if run.get("status") in TERMINAL_STATUSES else \
        {"working":"running","reaping":"reaping","exited":"exited","stale":"stale"}[liveness]
    print(json.dumps({**run,"status":status,"liveness":liveness},sort_keys=True))
if __name__=="__main__":
 try: main()
 except ValueError as e: print("resource-runner:",e,file=sys.stderr); raise SystemExit(65)
