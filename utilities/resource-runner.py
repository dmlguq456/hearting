#!/usr/bin/env python3
"""Detached process runner with PID reuse-safe reattachment."""
import argparse, contextlib, fcntl, hashlib, json, os, re, select, signal, subprocess, sys, time
from pathlib import Path
import resource_resume as RESOURCE_RESUME
import resource_run_evidence as RUN_EVIDENCE
from resource_progress import environment as progress_environment
import gpu_leases
from gpu_execution_sandbox import gpu_resource_nodes
from resource_run_registry import (
    classify_identity,
    is_alive,
    proc_identity,
    register_registry,
    resource_never_started,
    reboot_evidence,
)

SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
RETRY = re.compile(r"^(?P<base>.+)__a(?P<n>[1-9][0-9]*)$")
TERMINAL_STATUSES = {"succeeded", "failed"}

# The payload runs under a tiny POSIX-sh sentinel wrapper so its exit status survives
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
    raise SystemExit(65)
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
    keys = ("run_id", "cwd", "log", "command", "route", "node", "parent_attempt_id", "jobs",
            "config_ref", "config_sha256", "source_commit", "source_dirty", "source_git_state", "config_layout",
            "resource_policy", "owner_wait", "launch_request", "share")
    import dispatch_resource_wait as OWNER_RESOURCE
    sup = OWNER_RESOURCE.supervisor()
    ledger = sup.ledger_for(route, jobs) if route.get("route_id") else None
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
        current = data["runs"].get(args.run_id)
        if current is not None:
            if any(current.get(k) != placeholder.get(k) for k in keys):
                raise ValueError("resource-route-body-conflict")
            return False, current
        for previous in matches:
            if reboot_evidence(previous):
                settle(registry, previous['run_id'], previous, data=data)
        from route_authority import require_resource_predecessors
        require_resource_predecessors([*matches, *history], placeholder)
        data["runs"][args.run_id] = placeholder
        return True, placeholder
    with ledger.lock() if ledger else contextlib.nullcontext():
        if ledger and sup.resource_continuation_cancelled(route, ledger):
            raise ValueError("resource-parent-close-requested")
        history = [row for _, row in sup.resource_predecessors(ledger, args.node)] if ledger else []
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
            "declared_outputs": node.get("outputs", ["run.json"])}, prepare=True)
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
        launch_argv = ["/bin/sh", "-c", 'IFS= read -r launch <&"$AGENT_RESOURCE_LAUNCH_FD" || exit 125; '
                       + SENTINEL_SCRIPT, "resource-runner", *(controller.command if controller else placeholder["command"])]
        environment.update(progress_environment(placeholder))
        environment.update(HEARTING_GPU_LEASE_RELEASE="")
        environment.update(gpu_env)
        environment.update(HEARTING_RESOURCE_RUN_ID=args.run_id, HEARTING_RESOURCE_REGISTRY=str(registry.resolve()))
        environment.update(AGENT_RESOURCE_SENTINEL=str(sentinel), AGENT_RESOURCE_LAUNCH_FD=str(wait_read))
        try:
            with open(log, "ab", buffering=0) as output:
                proc = subprocess.Popen(launch_argv, cwd=placeholder["cwd"], env=environment,
                    pass_fds=(wait_read,), stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        finally:
            os.close(wait_read)
        ident = None
        for _ in range(20):
            ident = proc_identity(proc.pid)
            if ident:
                break
            if proc.poll() is not None:
                break
            time.sleep(.01)
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
                                   launch_state="not-started", ended_at=time.time(), exit_code=None)
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
        raise


def publish_verified_run(registry, run_id, expected, published):
    """Only the exact reserved row can release this payload; a foreign row survives."""
    def apply(data):
        if data["runs"].get(run_id) != expected:
            raise ValueError("resource-reservation-changed")
        data["runs"][run_id] = published
    locked_update(registry, apply)
def main(argv=None, *, controller=None):
    p=argparse.ArgumentParser(); p.add_argument("--registry"); s=p.add_subparsers(dest="cmd",required=True)
    a=s.add_parser("start"); a.add_argument("--run-id",required=True); a.add_argument("--cwd",required=True); a.add_argument("--log",required=True); a.add_argument("--route",required=True); a.add_argument("--node",required=True); a.add_argument("--smoke-attestation"); a.add_argument("--config-manifest")
    a.add_argument("--parent-attempt-id",help="registered headless attempt that owns this resource child")
    a.add_argument("--jobs", help="canonical dispatch registry for the existing continuation")
    a.add_argument("--share", action="store_true", help="Allow intentional GPU sharing")
    a.add_argument("command",nargs=argparse.REMAINDER)
    for name in ("status","stop","tail","reap"):
        x=s.add_parser(name); x.add_argument("--run-id",required=True)
    s.add_parser("list")
    index_cmd=s.add_parser("index"); index_cmd.add_argument("--registry",required=True)
    args=p.parse_args(argv)
    if args.cmd=="index":
        registry=Path(args.registry).resolve(strict=True)
        indexed=register_registry(registry)
        print(json.dumps({"registry":str(registry),**indexed},sort_keys=True))
        return
    if not args.registry: fail("--registry is required")
    registry=Path(args.registry).resolve()
    if args.cmd=="start":
        cwd=Path(args.cwd).resolve(strict=True)
        command=args.command[1:] if args.command[:1]==["--"] else args.command
        if not command: fail("command required")
        route_file=Path(args.route)
        if route_file.is_symlink():
            fail("route-file-unsafe")
        route_file=route_file.resolve(strict=True)
        route=json.loads(route_file.read_text())
        if route.get("capability") not in {"autopilot-code", "autopilot-lab"}:
            fail("route-capability-not-accepted")
        artifact_root=Path(str(route.get("artifact_root", ""))).resolve()
        if not route_file.is_relative_to(artifact_root):
            fail("route-file-outside-artifact-root")
        subprocess.run([
            sys.executable, str(Path(__file__).with_name("capability-route.py")),
            "verify", "--route", str(route_file), "--cwd", str(cwd),
        ], check=True, stdout=subprocess.DEVNULL)
        node=next((n for n in route["nodes"] if isinstance(n,dict) and n.get("id")==args.node),None)
        if not node or node.get("kind")!="resource-runner" or node.get("resource_transport")!="detached-process":
            fail("route node is not detached resource-runner")
        resume = RESOURCE_RESUME.route_selected(route) and node.get("resource_policy") == "verified-resume"
        if resume and not SAFE_RUN_ID.fullmatch(args.run_id): fail("invalid --run-id")
        if not args.smoke_attestation and not resume: fail("hash-bound smoke attestation required")
        if args.smoke_attestation:
            subprocess.run([sys.executable,str(Path(__file__).parents[1]/"tools/smoke-attestation.py"),"verify","--attestation",args.smoke_attestation],check=True)
        provenance = {}
        if args.config_manifest:
            manifest = json.loads(Path(args.config_manifest).read_text())
            verify_tool = Path(__file__).parents[1] / "tools" / "lab-config-provenance.py"
            subprocess.run([sys.executable, str(verify_tool), "verify", "--manifest", args.config_manifest], check=True)
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
        placeholder={"run_id":args.run_id,"cwd":str(cwd),"log":str(log),"command":command,
                     **provenance,"route":args.route,"node":args.node,"status":"launching",
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
                if owner_wait.get("launch_scope") == "codex-owner-controller":
                    placeholder["launch_request"] = launch_request(args)
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
        launch_argv=["/bin/sh","-c",'IFS= read -r launch <&"$AGENT_RESOURCE_LAUNCH_FD" || exit 125; '
                     + SENTINEL_SCRIPT,"resource-runner",*command]
        environment={**os.environ,**progress_environment(placeholder),"AGENT_RESOURCE_SENTINEL":str(sentinel),
                     "HEARTING_RESOURCE_RUN_ID": args.run_id, "HEARTING_RESOURCE_REGISTRY": str(registry.resolve()),
                     "HEARTING_GPU_LEASE_RELEASE": "", **gpu_env, "AGENT_RESOURCE_LAUNCH_FD": str(wait_read)}
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
        ident=None
        for _ in range(20):
            ident=proc_identity(proc.pid)
            if ident: break
            time.sleep(.01)
        if not ident:
            os.close(release)
            proc.kill()
            gpu_leases.release(lease_path, gpu_lease)
            locked_update(registry,lambda data:data["runs"].pop(args.run_id,None))
            fail("could not establish process identity")
        run={**ident,"pid_namespace":os.readlink("/proc/self/ns/pid"),"run_id":args.run_id,"process_group":os.getpgid(proc.pid),"cwd":str(cwd),"log":str(log),"command":command,
             "launch_argv":launch_argv,"sentinel":str(sentinel),"progress_file":placeholder["progress_file"],
             "parent_attempt_id":args.parent_attempt_id,**provenance,
             "route":args.route,"node":args.node,"status":"running","workflow_state":"RUNNING",
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
