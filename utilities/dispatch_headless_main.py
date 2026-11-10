#!/usr/bin/env python3
"""The launch phases of `main()` that the three adapter dispatch wrappers share.

`adapters/<harness>/bin/dispatch-headless.py` each ran the same governor
claim and the same claimed-attempt launch: fence, spawn, governor transfer,
watchers, foreground wait and settlement, early-death watch. Those steps now
live here once. A wrapper keeps only what its harness does differently and
passes it in a `LaunchSpec`.

Every wrapper-level name these phases call is looked up through the calling
wrapper's own module globals (`AdapterNamespace`) at call time, so a wrapper
can still supply its own variant of a helper and tests that patch a name on a
wrapper module keep reaching the code they patch.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


class AdapterNamespace:
    """Read a wrapper module's globals at call time."""

    __slots__ = ("_globals",)

    def __init__(self, module_globals: Mapping[str, Any]):
        self._globals = module_globals

    def __getattr__(self, name: str) -> Any:
        try:
            return self._globals[name]
        except KeyError:
            raise AttributeError(name) from None


def worker_environment(h, args, *, harness: str, jobs: Path, governor_root: Path,
                       reservation_token: str, permission_env: Mapping[str, str],
                       current_sandbox: str, supervised: bool,
                       extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The child environment every wrapper gives its claimed worker.

    The inherited environment minus broker variables, then the dispatch identity,
    route, artifact, governor and registry values. `permission_env` is the
    harness's runtime permission context, `extra` its own additional variables.
    """
    review = args.review_output_binding
    env = {key: value for key, value in os.environ.items() if not key.startswith("AGENT_DISPATCH_BROKER_")}
    env.update({
        "AGENT_SESSION_ROLE": "worker",
        "AGENT_DISPATCH_CHILD": "1",
        "AGENT_DISPATCH_DEPTH": str(args.dispatch_depth),
        "AGENT_DISPATCH_ATTEMPT_SCHEMA_VERSION": "2",
        "AGENT_DISPATCH_TRANSPORT": "headless",
        "AGENT_DISPATCH_EXECUTION_SURFACE": args.execution_surface,
        "AGENT_DISPATCH_REGISTERED_WORKER": str(int(bool(args.registered_worker))),
        "AGENT_DISPATCH_FALLBACK_HOP": args.fallback_hop,
        "AGENT_DISPATCH_PARENT_CWD": (
            h._effective_parent_cwd(args) if (args.parent_slug or args.parent_session_id) else ""
        ),
        "AGENT_DISPATCH_INTENSITY": args.intensity,
        "AGENT_DISPATCH_CAPABILITY_MODE": args.capability_mode,
        # This session's own slug — the conductor Stop gate / dispatch-wait
        # identify "open child rows whose parent= equals MY slug" and cannot
        # do so from AGENT_DISPATCH_PARENT_SLUG (which points at the parent).
        "AGENT_DISPATCH_SELF_SLUG": args.slug,
        "AGENT_DISPATCH_PARENT_SLUG": args.parent_slug or "",
        "AGENT_DISPATCH_ATTEMPT_ID": args.attempt_id,
        "AGENT_DISPATCH_PARENT_ATTEMPT_ID": args.parent_attempt_id or "",
        "AGENT_DISPATCH_PARENT_SESSION_ID": args.parent_session_id or "",
        "AGENT_DISPATCH_WORKER_TYPE": args.worker_type,
        **permission_env,
        "AGENT_DISPATCH_ASSIGNED_CONTRACT": args.assigned_contract,
        "AGENT_DISPATCH_OWNER": args.capability_owner or "",
        "AGENT_DISPATCH_OWNER_HARNESS": args.owner_harness or "",
        "AGENT_ARTIFACT_ROOT": args.artifact_root,
        "AGENT_DISPATCH_WORKTREE": review["worktree"] if review else args.worktree,
        "AGENT_REVIEW_OUTPUT": review["output_path"] if review else "",
        "AGENT_REVIEW_CYCLE_ID": review["cycle_id"] if review else "",
        "AGENT_REVIEW_PRODUCER_ID": review["producer_id"] if review else "",
        # W7C producer lifecycle: the owner's open cycle (issued by
        # `artifact_producer.py begin` before the first write) is passed
        # through unchanged so stage workers write into the same
        # `campaigns/<camp>/cycles/<cyc>/artifacts/` and never issue a
        # second lineage.
        **h.artifact_cycle_environment(os.environ),
        "AGENT_RUNTIME_DIR": getattr(args, "runtime_dir", ""),
        "REPORT_BUNDLE_ROOT": str(args.report_bundle_root or ""),
        "AGENT_ROUTE_FILE": (
            args.route_file
            or (args.owner_route_binding.route_file if args.owner_route_binding else "")
        ),
        "AGENT_ROUTE_ID": (
            args.route_id
            or (args.owner_route_binding.route_id if args.owner_route_binding else "")
        ),
        "AGENT_ROUTE_NODE": args.route_node or "",
        "AGENT_MODEL_GOVERNOR_ROOT": str(governor_root),
        h.GOVERNOR_RESERVATION_ENV: reservation_token,
        "AGENT_HOME": str(args.agent_home),
        "AGENT_DISPATCH_JOBS": str(jobs),
        # A worker inherits AGENT_HOME at the managed release; keep its
        # bytecode out of that immutable tree (defect Q pairing).
        **h.bytecode_cache_env(),
        **h.parent_completion.worker_runtime_identity(harness),
        "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
        "AGENT_DISPATCH_CURRENT_SANDBOX": current_sandbox,
        **h.stage_session_environment(args),
        "AGENT_DISPATCH_COMPLETION_MODE": "supervised" if supervised else "poll",
        **(extra or {}),
    })
    return env


def apply_worker_fields(env: dict, args, *, worker_mode: bool = True) -> None:
    """Publish the grant file and the optional role, mode and unit; clear the absent ones."""
    if args.execution_access_grant is not None:
        env["AGENT_DISPATCH_EXECUTION_ACCESS_FILE"] = str(args.execution_access_grant.source_path)
    fields = [("AGENT_DISPATCH_WORKER_ROLE", args.worker_role)]
    if worker_mode:
        fields.append(("AGENT_DISPATCH_WORKER_MODE", args.worker_mode))
    fields.append(("AGENT_DISPATCH_UNIT", args.unit))
    for key, value in fields:
        if value:
            env[key] = value
        else:
            env.pop(key, None)


@dataclass
class GovernorClaim:
    """The model governor claim of one registered attempt.

    `rc` is set when the wrapper must return it before any launch.
    """

    rc: int | None
    governor: Path | None = None
    governor_root: Path | None = None
    reservation_token: str = ""


def claim_registered_attempt(h, args, action: str, *, jobs: Path, prompt_path: Path,
                             prompt_text: str,
                             before_claim: Callable[[], int | None] | None = None) -> GovernorClaim:
    """Reserve the governor slot, register the row, write the prompt and bind the owner.

    Register before launch so harvest can always reclaim the attempt even if the
    launch itself never comes up. `before_claim` runs first for registering
    actions and may stop the launch with a return code.
    """
    governor = h.ROOT / "utilities" / "model-worker-governor.py"
    try:
        governor_root = h.resolve_model_governor_root(args.artifact_root)
    except h.DispatchContractError as exc:
        return GovernorClaim(h.fail(exc.reason, 73, detail=exc.detail, child_spawned="0"))
    claim = GovernorClaim(None, governor, governor_root, "")
    args.replica_batch_reservation = {}
    if action in ("register", "start"):
        if before_claim is not None:
            rc = before_claim()
            if rc is not None:
                claim.rc = rc
                return claim
        if action == "start":
            try:
                claim.reservation_token, args.replica_batch_reservation = h.reserve_governor_token(
                    governor,
                    governor_root,
                    "dispatch",
                    provided_token=os.environ.get(h.GOVERNOR_RESERVATION_ENV, ""),
                    expected_reservation=args.replica_batch_expectation,
                )
            except h.DispatchContractError as exc:
                claim.rc = h.fail(exc.reason, 75, detail=exc.detail, child_spawned="0",
                                  **h.governor_refusal_fields(exc))
                return claim
        reservation_token = claim.reservation_token
        try:
            args.attempt_claimed = h.append_job(jobs, args)
            if action == "start" and not args.attempt_claimed:
                args.attempt_claimed = h.attempt_launch_is_available(
                    jobs, args.attempt_id
                )
            h.parent_completion.validate_registered_delivery(args, jobs, read=h.registered_parent_delivery)
        except h.DispatchContractError as e:
            h.cancel_governor_reservation(governor, governor_root, reservation_token)
            claim.rc = h.fail(e.reason, 73, detail=e.detail, child_spawned="0")
            return claim
        except h.ManagedDispatchError as e:
            h.cancel_governor_reservation(governor, governor_root, reservation_token)
            claim.rc = h.fail(str(e), 73, child_spawned="0")
            return claim
        if args.attempt_claimed:
            h.initialize_supervised_owner_input(args, jobs)
            try:
                Path(h.frame_repository.execution_path(prompt_path)).write_text(prompt_text, encoding="utf-8")
            except OSError as exc:
                h.annotate_attempt_row(
                    jobs, args.attempt_id, {"launch_outcome": "never-launched"}
                )
                h.cancel_governor_reservation(governor, governor_root, reservation_token)
                h.close_job_row(
                    jobs, args.slug, args.worktree,
                    "prompt-materialization-failed", "", args.attempt_id,
                )
                claim.rc = h.fail(
                    "prompt-materialization-failed", 73,
                    detail=str(exc), child_spawned="0",
                )
                return claim
            if action == "start":
                try:
                    h.bind_owner_launch(args, jobs)
                except h.ProducerError as exc:
                    h.annotate_attempt_row(jobs, args.attempt_id, {"launch_outcome": "never-launched"})
                    h.cancel_governor_reservation(governor, governor_root, reservation_token)
                    h.close_job_row(jobs, args.slug, args.worktree, "producer-binding-failed", "", args.attempt_id)
                    claim.rc = h.fail(exc.code, 73, detail=exc.detail, child_spawned="0")
                    return claim
    else:
        args.attempt_claimed = False

    if action == "start" and not args.attempt_claimed:
        h.cancel_governor_reservation(governor, governor_root, claim.reservation_token)
    return claim


@dataclass
class LaunchSpec:
    """What one harness does differently while launching a claimed attempt."""

    harness: str
    runtime_sandbox: Callable[[], str]
    grant_runtime: Callable[[], str]
    grant_kwargs: Callable[[], dict]
    metadata_head: Callable[[dict], dict] | None = None
    metadata_after_scope: Callable[[dict, dict], None] | None = None
    scope_kwargs: Callable[[], dict] | None = None
    metadata_after_grant: Callable[[dict, dict], None] | None = None
    parent_exit_launch_reason: bool = True
    launch_failure_child_spawned: bool = True
    after_foreground_settle: Callable[[Any, dict, bool], None] | None = None


def launch_claimed_attempt(h, spec: LaunchSpec, args, *, jobs: Path, env: dict, command: str,
                           claim: GovernorClaim, log_path: Path, prompt_path: Path,
                           agent_home: Path, default_roots) -> int | None:
    """Launch a claimed attempt and follow it to its lifecycle's return point.

    Returns the wrapper's exit code when the launch stops early, else None.
    """
    governor, governor_root = claim.governor, claim.governor_root
    reservation_token = claim.reservation_token
    h.launch_parent_completion_sidecar(args, jobs)
    if args.managed_sidecar_state == "launch-failed":
        h.annotate_attempt_row(
            jobs, args.attempt_id, {"launch_outcome": "never-launched"}
        )
        h.cancel_governor_reservation(
            governor, governor_root, reservation_token
        )
        h.close_job_row(
            jobs,
            args.slug,
            args.worktree,
            "managed-sidecar-launch-failed",
            "",
            args.attempt_id,
        )
        return h.fail(
            "managed-sidecar-launch-failed",
            75,
            detail=args.managed_sidecar_reason,
            attempt_id=args.attempt_id,
            child_spawned="0",
        )
    fence_failure_read_fd, fence_failure_write_fd = os.pipe()
    args.watchdog_budget = h.begin_finite_watchdog(args.foreground_timeout)
    def spawn_worker(gate_fd: int) -> subprocess.Popen:
        fence_command = [
            sys.executable, str(h.ROOT / "utilities" / "launch-fence.py"),
            "--parent-pid", str(os.getpid()),
            "--gate-fd", str(gate_fd),
            "--failure-fd", str(fence_failure_write_fd),
            "--jobs", str(jobs), "--attempt-id", args.attempt_id,
        ]
        if args.route_file:
            fence_command.extend(
                [
                    "--route-file", args.route_file,
                    "--launch-phase", args.action,
                ]
            )
        fence_command.extend(
            [
                "--post-release-parent-death-signal",
                "kill" if args.launch_lifecycle == h.FOREGROUND_SCOPED else "none",
                "--",
                sys.executable, str(governor), "--root", str(governor_root),
                "run", "--class", "dispatch", "--", "sh", "-c", command,
            ]
        )
        if args.review_output and args.launch_lifecycle == h.DETACHED:
            try:
                args.review_watchdog_handle = h.launch_review_watchdog(
                    fence_command, gate_fd=gate_fd, budget=args.watchdog_budget,
                    attempt_id=args.attempt_id,
                    nonce=args.review_governed_lease_nonce,
                    failure_fd=fence_failure_write_fd, env=env,
                    lease_release_spec={
                        "root": args.artifact_root,
                        "cycle_id": args.review_output_binding["cycle_id"],
                        "attempt_id": args.attempt_id,
                    }, jobs=jobs,
                )
            finally:
                try:
                    os.close(fence_failure_write_fd)
                except OSError:
                    pass
            return args.review_watchdog_handle.process
        try:
            return subprocess.Popen(
                fence_command,
                env=h.frame_repository.child_environment(env),
                start_new_session=True,
                **h.frame_repository.child_process_options(pass_fds=(gate_fd, fence_failure_write_fd)),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        finally:
            try:
                os.close(fence_failure_write_fd)
            except OSError:
                pass
    launch_metadata = {
        **args.launch_lifecycle_resolution.metadata(),
        "runtime_sandbox": spec.runtime_sandbox(),
        **(spec.metadata_head(env) if spec.metadata_head else {}),
    }
    from dispatch_capacity_evidence import launch_scope
    launch_metadata.update(launch_scope(spec.harness, env))
    if spec.metadata_after_scope:
        spec.metadata_after_scope(launch_metadata, env)
    grant_route_id, grant_route_hash = args.route_id, args.route_hash
    if not grant_route_id and not grant_route_hash and args.owner_route_binding:
        grant_route_id = args.owner_route_binding.route_id
        grant_route_hash = args.owner_route_binding.route_hash
    if grant_route_id and grant_route_hash:
        from verification_runtime import effective_scope
        local_verification = effective_scope(args, [*default_roots,
            *(args.execution_access_grant.writable_roots if args.execution_access_grant else ())],
            spec.harness, env, **(spec.scope_kwargs() if spec.scope_kwargs else {}))
        effective_path, effective_sha256 = h.publish_effective_grant(
            jobs=jobs, attempt_id=args.attempt_id, route_id=grant_route_id,
            route_hash=grant_route_hash,
            runtime=spec.grant_runtime(), sandbox=spec.runtime_sandbox(),
            grant=args.execution_access_grant,
            default_writable_roots=default_roots,
            verification_scope=local_verification,
            **spec.grant_kwargs(),
        )
        launch_metadata["execution_access_effective_file"] = str(effective_path)
        launch_metadata["execution_access_effective_sha256"] = effective_sha256
    if spec.metadata_after_grant:
        spec.metadata_after_grant(launch_metadata, env)
    if args.dispatch_depth >= 2 and os.environ.get("AGENT_DISPATCH_CHILD") == "1":
        launch_metadata["pid_scope"] = "namespace-local"
    # Apply after the masked home and every nested environment merge.
    env.update(h.git_optional_locks_env())
    try:
        proc, launch_metadata = h.spawn_claimed_attempt(
            jobs,
            args.attempt_id,
            parent_binding=args.parent_binding,
            spawn=spawn_worker,
            launch_metadata=launch_metadata,
            preclaim=getattr(args, "launch_preclaim", None),
            pre_release=lambda identity: h.attach_summary_owner(
                args, log_path, prompt_path, identity
            ),
            post_claim=lambda identity: h.acquire_review_lease_after_claim(args, jobs, identity),
        )
    except h.DispatchContractError as exc:
        for fd in (fence_failure_read_fd, fence_failure_write_fd):
            try:
                os.close(fd)
            except OSError:
                pass
        if exc.reason == "attempt-launch-already-claimed":
            h.cancel_governor_reservation(
                governor, governor_root, reservation_token
            )
            print("check=ok")
            print("status=start")
            print(f"attempt_id={args.attempt_id}")
            print("duplicate_attempt=1")
            # The claimed row says nothing about whether its process still
            # runs; the shared core reads that from the process itself.
            existing_state, existing_reason = h.existing_attempt_launch_state(
                jobs, args.attempt_id
            )
            print(f"launch_state={existing_state}")
            print("registered=0")
            print("started=0")
            print("child_spawned=0")
            print(f"reason={existing_reason}")
            if existing_state in h.EXISTING_ATTEMPT_NOTES:
                print(f"note={h.EXISTING_ATTEMPT_NOTES[existing_state]}")
            return 0
        reason = (
            "parent-exited"
            if spec.parent_exit_launch_reason and exc.reason.startswith("parent-attempt-")
            else (
                "summary-owner-launch-failed"
                if exc.reason.startswith("attempt-pre-release-")
                else "launch-error"
            )
        )
        outcome = h.adapter_launch_failure_outcome(jobs, args.attempt_id, exc.reason)
        h.annotate_attempt_row(jobs, args.attempt_id, {"launch_outcome": outcome})
        h.cancel_governor_reservation(governor, governor_root, reservation_token)
        h.close_job_row(jobs, args.slug, args.worktree, reason, "", args.attempt_id)
        if not spec.launch_failure_child_spawned:
            return h.fail(exc.reason, 73, detail=exc.detail, attempt_id=args.attempt_id)
        return h.fail(
            exc.reason, 73, detail=exc.detail,
            attempt_id=args.attempt_id,
            child_spawned="1" if outcome == "post-release-failed" else "0",
        )
    except OSError as exc:
        for fd in (fence_failure_read_fd, fence_failure_write_fd):
            try:
                os.close(fd)
            except OSError:
                pass
        h.annotate_attempt_row(
            jobs, args.attempt_id, {"launch_outcome": "never-launched"}
        )
        h.cancel_governor_reservation(governor, governor_root, reservation_token)
        h.close_job_row(jobs, args.slug, args.worktree, "launch-error", "", args.attempt_id)
        return h.fail("child-launch-failed", 70, detail=str(exc), attempt_id=args.attempt_id)
    try:
        args.governor_reservation = h.wait_governor_reservation_claim(
            governor,
            governor_root,
            reservation_token,
            proc,
            expected_reservation=args.replica_batch_expectation,
            watchdog_receipt=(args.review_watchdog_handle.receipt
                              if getattr(args, "review_watchdog_handle", None) else None),
        )
    except h.DispatchContractError as exc:
        fence_failure, fence_released = h.read_launch_fence_failure(
            fence_failure_read_fd
        )
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=5.0 if getattr(args, "review_watchdog_handle", None) else 0.5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5.0 if getattr(args, "review_watchdog_handle", None) else 0.5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
        h.cancel_governor_reservation(governor, governor_root, reservation_token)
        if fence_failure is not None:
            reason = str(fence_failure["reason"])
            # The fence knows exactly which sealed root disagreed with the
            # live one. This is a diagnostic on a path that is ALREADY
            # failing, so it can never be allowed to prevent the row from
            # closing: a raise here would leak the attempt open with a sealed
            # launch_home, which then pins its release against pruning.
            annotation = {"launch_outcome": "never-launched"}
            try:
                annotation.update(
                    h.launch_mismatch_annotation(str(fence_failure["detail"]))
                )
                h.annotate_attempt_row(jobs, args.attempt_id, annotation)
            except h.DispatchContractError:
                h.annotate_attempt_row(
                    jobs, args.attempt_id, {"launch_outcome": "never-launched"}
                )
            h.close_job_row(
                jobs, args.slug, args.worktree, reason, "", args.attempt_id,
            )
            return h.fail(
                reason, 73, detail=str(fence_failure["detail"]),
                attempt_id=args.attempt_id, registered="0", started="0",
                child_spawned="0",
            )
        post_exit = h.deterministic_post_exit_outcome(
            proc, fence_released=fence_released,
        )
        if post_exit.launch_outcome:
            h.annotate_attempt_row(
                jobs,
                args.attempt_id,
                {
                    key: value
                    for key, value in {
                        "launch_outcome": post_exit.launch_outcome,
                        "group_reap_proof": post_exit.group_reap_proof,
                        "group_reap_pgid": post_exit.group_reap_pgid,
                    }.items()
                    if value
                },
            )
        h.close_job_row(
            jobs, args.slug, args.worktree,
            "governor-reservation-transfer", "", args.attempt_id,
        )
        return h.fail(
            exc.reason, 75, detail=exc.detail,
            attempt_id=args.attempt_id, child_spawned="1",
        )
    h.read_launch_fence_failure(fence_failure_read_fd)
    # Shared-worktree aliasing (OPERATIONS §5.10 signal order ①): record
    # the child pid so liveness can use a process signal. Conductor activity
    # in the same worktree can contaminate transcript mtime.
    foreground_review_handoff = (
        getattr(args, "launch_lifecycle", h.DETACHED) == h.FOREGROUND_SCOPED
        and getattr(args, "dispatch_depth", None) == 1
        and getattr(args, "worker_type", None) == "review"
        and getattr(args, "execution_surface", None) == "registered-headless"
        and bool(getattr(args, "registered_worker", False))
        and not any(h.foreground_review_launch_identity(args).values())
    )
    start_ticks = launch_metadata.get("pid_start", "")
    if (args.dispatch_depth == 1 and args.worker_type == "owner"
            and args.launch_lifecycle == h.DETACHED):
        try:
            watcher_pid = h.launch_orphan_watch(
                jobs, agent_home, args.attempt_id, proc.pid, start_ticks or "")
        except (h.DispatchContractError, OSError) as exc:
            return h.WRAPPER_COMMON.watcher_launch_failure(
                jobs, args.attempt_id, proc, launch_metadata,
                "orphan-watch-launch-error",
                getattr(exc, "reason", "orphan-watch-launch-failed"),
                getattr(exc, "detail", str(exc)),
                materialize=h.materialize_after_terminal_close, fail=h.fail,
            )
        h.annotate_job_row(
            jobs, args.slug, args.worktree,
            f"orphan_watch=post-exit,orphan_watch_pid={watcher_pid}",
            args.attempt_id,
        )
    if args.launch_lifecycle == h.DETACHED:
        try:
            reap_watch_pid = h.launch_reap_watch(
                jobs,
                args.attempt_id,
                proc.pid,
                start_ticks or "",
                int(launch_metadata.get("pgid", "0")),
            )
        except (h.DispatchContractError, ValueError, OSError) as exc:
            return h.WRAPPER_COMMON.watcher_launch_failure(
                jobs, args.attempt_id, proc, launch_metadata,
                "reap-watch-launch-error",
                getattr(exc, "reason", "reap-watch-launch-failed" if isinstance(exc, OSError)
                        else "reap-watch-identity-invalid"),
                getattr(exc, "detail", str(exc)),
                materialize=h.materialize_after_terminal_close, fail=h.fail,
            )
        h.annotate_attempt_row(
            jobs,
            args.attempt_id,
            {"reap_watch": "post-exit", "reap_watch_pid": str(reap_watch_pid)},
        )
    args.child_pid = proc.pid
    args.child_pid_start = start_ticks
    args.launch_heartbeat = h.seed_launch_heartbeat(args, jobs, proc.pid, start_ticks)
    activity_context = None
    if args.launch_lifecycle == h.FOREGROUND_SCOPED and args.worker_type != "owner":
        try:
            from foreground_activity import build_activity_context
            activity_context = build_activity_context(
                worker_type=args.worker_type, harness=spec.harness, log_path=log_path,
                jobs=jobs, launch_metadata=launch_metadata, attempt_id=args.attempt_id,
                route_id=args.route_id, route_node=args.route_node,
                environment=env, worktree=args.worktree,
            )
        except Exception as exc:
            print(f"warning=foreground activity context unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
    if args.launch_lifecycle == h.FOREGROUND_SCOPED and foreground_review_handoff:
        binding = args.parent_binding
        try:
            outcome = h.wait_foreground(
                proc,
                args.foreground_timeout,
                watchdog_budget=args.watchdog_budget,
                activity_context=activity_context,
                parent_pid=binding.observed_pid if binding else None,
                parent_pid_start=binding.observed_pid_start if binding else None,
                parent_is_live=(
                    (lambda: h.parent_attempt_binding_is_live(jobs, binding))
                    if binding else None
                ),
            )
            foreground_seal = h.seal_foreground_result(
                jobs, args.attempt_id, proc.pid, start_ticks or "",
                int(launch_metadata.get("pgid", "0")),
                exit_code=outcome.exit_code,
                failure=outcome.failure,
                group_empty=outcome.group_empty,
            )
        except (h.DispatchContractError, ValueError) as exc:
            return h.WRAPPER_COMMON.watcher_launch_failure(
                jobs, args.attempt_id, proc, launch_metadata,
                getattr(exc, "reason", "foreground-outcome-seal-error"),
                getattr(exc, "reason", "foreground-outcome-seal-error"),
                getattr(exc, "detail", str(exc)),
                materialize=h.materialize_after_terminal_close, fail=h.fail,
            )
        try:
            reap_watch_pid = h.launch_reap_watch(
                jobs, args.attempt_id, proc.pid, start_ticks or "",
                int(launch_metadata.get("pgid", "0")),
                foreground_seal=foreground_seal,
            )
        except (h.DispatchContractError, ValueError, OSError) as exc:
            return h.WRAPPER_COMMON.watcher_launch_failure(
                jobs, args.attempt_id, proc, launch_metadata,
                "reap-watch-launch-error",
                getattr(exc, "reason", "reap-watch-launch-failed" if isinstance(exc, OSError)
                        else "foreground-outcome-seal-error"),
                getattr(exc, "detail", str(exc)),
                materialize=h.materialize_after_terminal_close, fail=h.fail,
            )
        h.annotate_attempt_row(
            jobs, args.attempt_id,
            {"reap_watch": "post-exit", "reap_watch_pid": str(reap_watch_pid)},
        )
        args.worker_exit = outcome.exit_code
        args.worker_failure = outcome.failure
    elif args.launch_lifecycle == h.FOREGROUND_SCOPED:
        binding = args.parent_binding
        outcome = h.wait_foreground(
            proc,
            args.foreground_timeout,
            parent_pid=binding.observed_pid if binding else None,
            parent_pid_start=binding.observed_pid_start if binding else None,
            parent_is_live=(
                (lambda: h.parent_attempt_binding_is_live(jobs, binding))
                if binding
                else None
            ),
            watchdog_budget=args.watchdog_budget,
            activity_context=activity_context,
        )
        h.annotate_attempt_row(
            jobs,
            args.attempt_id,
            (
                {
                    "launch_outcome": "governed-process-reaped",
                    "group_reap_proof": h.GROUP_REAP_PROOF,
                    "group_reap_pgid": str(proc.pid),
                }
                if outcome.group_empty
                else {"launch_outcome": "governed-process-reap-unverified"}
            ),
        )
        args.worker_exit = outcome.exit_code
        args.worker_failure = outcome.failure
        settled = h.settle_foreground_exit(
            jobs, args.attempt_id, log_path, outcome, worktree=args.worktree,
            artifact_root=args.artifact_root, worker_type=args.worker_type,
            legacy_close=lambda failure: h.close_job_row(
                jobs, args.slug, args.worktree, failure, "", args.attempt_id),
        )
        terminal = settled["inspection"]
        args.terminal_inspection = terminal
        args.terminal_verdict = settled["verdict"]
        args.worker_failure = settled["worker_failure"]
        terminal_closed = settled["closed"]
        if spec.after_foreground_settle:
            spec.after_foreground_settle(outcome, terminal, terminal_closed)
    else:
        # SD-15: detached launches retain the short early-death watch.
        death = h.watch_early_death(proc, log_path, args.early_exit_watch)
        if death:
            reason, reset = death
            h.close_job_row(jobs, args.slug, args.worktree, reason, reset, args.attempt_id)
            if reason != "capacity":
                h.write_reset_cache(agent_home, spec.harness, reason, reset, jobs)
            args.early_death = (reason, reset)
    return None
