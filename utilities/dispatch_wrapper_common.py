#!/usr/bin/env python3
"""Helpers the three adapter dispatch wrappers carried as identical copies.

Each `adapters/<harness>/bin/dispatch-headless.py` had these functions
letter for letter (audit §4 #10): failure lines, the registry lock, process
start ticks, the launch fence's failure record, the artifact and report
bundle roots, the launch heartbeat seed, a route node's leg fields, the
supervised owner's route, and the review output request. None of them reads
anything about a harness. The wrappers keep their old names as aliases, so
callers and tests that patch a wrapper's name are unchanged.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from artifact_producer import ProducerError, prepare_review_output_binding
from dispatch_contract import (
    REPLICA_RESERVATION_ROW_KEYS, SUPERVISOR_LEASE_KIND, DispatchContractError, diff_attribution_lines,
    dispatch_state_root, dispatch_state_roots, resolve_dispatch_state_root, runtime_ancestry_binding,
    sealed_launch_home, source_lineage_row_fields, supervisor_lease_path, workflow_completion_receipt,
    ensure_terminal_claim_absent,
)
import commit_policy
import dispatch_parent_completion as parent_completion
from execution_access import receipt_fragment as execution_access_receipt_fragment
from stage_session_runtime import metadata as stage_session_metadata
from model_config import ModelConfigError, resolve_config
from route_authority import scan_anchored_death

ROOT = Path(__file__).resolve().parents[1]


def fail(reason: str, code: int, **fields: str) -> int:
    print("check=failed")
    print(f"reason={reason}")
    for key, value in fields.items():
        print(f"{key}={value}")
    return code


def read_launch_fence_failure(fd: int) -> tuple[dict[str, object] | None, bool]:
    """Read and close the fence's private, close-on-exec failure channel.

    Returns the parsed failure record (or None) alongside whether the fence
    was actually released: `BlockingIOError` means the write end is still
    open (the child has not reached the fence yet, so nothing was released),
    while an EOF read means the write end already closed (the fence was
    released with no failure payload).
    """
    try:
        os.set_blocking(fd, False)
        try:
            raw = os.read(fd, 16384)
        except BlockingIOError:
            return None, False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    if not raw:
        return None, True
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, True
    if (
        not isinstance(record, dict)
        or record.get("schema_version") != 1
        or not isinstance(record.get("reason"), str)
        or not isinstance(record.get("detail"), str)
    ):
        return None, True
    return record, True


def resolve_artifact_root(worktree: str) -> str:
    result = subprocess.run(
        [str(ROOT / "utilities" / "artifact-root.sh"), worktree],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or not value or not Path(value).is_absolute():
        detail = (result.stderr or result.stdout or "invalid artifact root").strip()
        raise ValueError(detail)
    return value


def is_report_bundle_publish_stage(route_file: str | None, route_node: str | None) -> bool:
    if not route_file or route_node != "publish":
        return False
    try:
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    expected = {
        "id": "publish", "kind": "capability-owner", "unit": "_kernel/owner",
        "completion_gate": "lab-publish", "dispatch_depth": 1,
    }
    return route.get("capability") == "autopilot-lab" and any(
        all(node.get(key) == value for key, value in expected.items())
        for node in route.get("nodes", []) if isinstance(node, dict)
    )


def resolve_report_bundle_root(route_file: str | None, route_node: str | None) -> Path | None:
    if not is_report_bundle_publish_stage(route_file, route_node):
        return None
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "report-bundle.py"), "root", "--optional"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    value = result.stdout.strip()
    if result.returncode != 0:
        raise ValueError((result.stderr or result.stdout or "invalid report bundle root").strip())
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise ValueError("configured report bundle root is not a safe directory")
    return path


def supervisor_route(args: argparse.Namespace) -> tuple[str, str, str] | None:
    """The route a supervised owner is bound to: the standard+ owner binding, or a
    quick owner's own one-shot tuple, never a partial one."""
    binding = getattr(args, "owner_route_binding", None)
    if binding:
        return binding.route_file, binding.route_id, binding.route_hash
    route = tuple(getattr(args, key, None) for key in ("route_file", "route_id", "route_hash"))
    if all(route) and getattr(args, "route_node", None) == "one-shot":
        return route
    return None


@contextmanager
def jobs_lock(jobs: Path):
    jobs.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(f"{jobs}.lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield lock_path
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def route_node_leg_fields(args):
    """Read the sealed leg_class/auxiliary_check off this wrapper's route node.

    W1c projection source: the fields are stamped by the compiler during
    parallel-group expansion, so the wrapper reads its own sealed node instead
    of trusting a second, independently-produced value. Missing node/fields
    project the explicit absence marker `-`.
    """
    route_file = getattr(args, "route_file", None)
    route_node = getattr(args, "route_node", None)
    if not route_file or not route_node:
        return "-", "-"
    try:
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "-", "-"
    for node in route.get("nodes", []):
        if isinstance(node, dict) and node.get("id") == route_node:
            return (
                str(node.get("leg_class") or "-"),
                str(node.get("auxiliary_check") or "-"),
            )
    return "-", "-"


def prepare_review_output_request(args) -> None:
    args.review_output_binding = None
    args.review_governed_lease_nonce = ""
    if not args.review_output:
        return
    if (
        args.dispatch_depth != 1
        or args.worker_type != "review"
        or args.unit != "qa/code-review"
        or args.capability != "autopilot-code"
        or args.execution_surface != "registered-headless"
        or not args.registered_worker
        or args.route_file
        or getattr(args, "owner_route_binding", None)
    ):
        raise ProducerError("review-output-tuple-invalid")
    cycle_id = os.environ.get("AGENT_ARTIFACT_CYCLE_ID", "")
    producer_id = os.environ.get("AGENT_ARTIFACT_PRODUCER_ID", "")
    if not cycle_id or not producer_id:
        raise ProducerError("review-output-cycle-binding-missing")
    args.review_output_binding = prepare_review_output_binding(
        Path(args.artifact_root), cycle_id=cycle_id,
        producer_id=producer_id, attempt_id=args.attempt_id,
        review_output=args.review_output, capability=args.capability,
        unit=args.unit, worktree=args.worktree,
    )
    args.review_governed_lease_nonce = secrets.token_hex(32)


def process_start_ticks(pid: int) -> str:
    try:
        return (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8").split()[21]
    except (OSError, IndexError):
        return ""


def seed_launch_heartbeat(args: argparse.Namespace, jobs: Path, pid: int, start: str) -> str:
    if not (args.attempt_id and args.route_id and args.route_node):
        return "not-route-bound"
    result = subprocess.run(
        [sys.executable, str(ROOT / "utilities/dispatch-progress.py"), "heartbeat",
         "--attempt-id", args.attempt_id, "--route-id", args.route_id,
         "--route-node", args.route_node, "--jobs", str(jobs),
         "--phase", "launch", "--kind", "registry",
         "--evidence", f"pid={pid};start={start or '-'}"],
        cwd=ROOT, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=False,
    )
    return "ok" if result.returncode == 0 else "failed"


def model_config_state(harness: str) -> tuple[str, str]:
    """Which models.conf this launch resolved (`user` or `shipped`) and why --
    on the receipt so a user copy silently replaced by the shipped file is
    visible (top review B2)."""

    try:
        _values, receipt = resolve_config(harness, source_root=ROOT)
    except ModelConfigError as exc:
        return "unavailable", str(exc)[:80]
    return receipt.source, receipt.reason


def initialize_owner_input_when(args: argparse.Namespace, jobs: Path, *, supervised: bool, input_kind: str) -> None:
    """Open correction admission at registration; without it `correct` stays unsupported."""
    if not supervised:
        return
    try:
        from dispatch_owner_input import initialize_owner_input
        initialize_owner_input(jobs, args.attempt_id, input_kind)
    except Exception as exc:
        sys.stderr.write(f"owner-input-init-skipped attempt_id={args.attempt_id} reason={type(exc).__name__}\n")


def watch_early_death(
    proc: subprocess.Popen, log_path: Path, watch_secs: float
) -> tuple[str, str] | None:
    """SD-15: poll a just-launched child for a limit/auth early death.

    Returns (reason, reset) if the child exits within watch_secs and its log tail
    matches a DEATH_PATTERN. SD-59 capacity is the one proactive exception: an
    anchored live capacity line interrupts the exact process group for failover.
    Otherwise returns None. Polls in 0.5s steps.
    """
    if watch_secs <= 0:
        return None
    deadline = time.monotonic() + watch_secs
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        try:
            live_tail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        except OSError:
            live_tail = ""
        live_death = scan_anchored_death(live_tail)
        if live_death and live_death[0] == "capacity":
            try:
                os.killpg(proc.pid, signal.SIGINT)
                proc.wait(timeout=2)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
            return live_death
        time.sleep(0.5)
    if proc.poll() is None:
        return None  # still alive past the watch window — not an early death
    try:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
    except OSError:
        tail = ""
    death = scan_anchored_death(tail)
    if death:
        return death
    if proc.returncode:
        return f"launch-exit-{proc.returncode}", ""
    return None


def bind_internal_eligibility_probe(args: argparse.Namespace, harness: str) -> None:
    """SD-66 fix-forward: run the nested-eligibility probe in-wrapper when a
    dispatch-depth-2 ``--start`` carries no explicit evidence, instead of failing
    closed on missing flags a caller never had reason to supply by hand.

    Triggers only when both evidence options are still at their parser
    default (``unknown``/empty) and the parent identity needed to run the
    probe is fully known. Explicit supported/unsupported/unknown/partial
    evidence, dispatch-depth-1, and dry-run/register never reach this function's
    trigger path (callers gate on depth/action before calling it). The probe's
    own JSON status is trusted only when every identity field it echoes back
    matches the request; a malformed/mismatched/erroring probe leaves
    ``nested_eligibility`` at its unknown default so `validate_nested_eligibility`
    still fails closed.
    """
    if args.dispatch_depth < 2 or args.action != "start":
        return
    if getattr(args, "nested_eligibility_explicit", False):
        return
    if args.nested_eligibility != "unknown" or args.eligibility_source:
        return
    if not all((args.parent_harness, args.parent_transport, args.parent_sandbox, args.launch_authority)):
        return
    if "unknown" in (args.parent_harness, args.parent_transport, args.parent_sandbox):
        return
    args.eligibility_probe = "internal"
    probe = ROOT / "utilities" / "nested-dispatch-eligibility.py"
    result = subprocess.run(
        [
            sys.executable, str(probe),
            "--parent-harness", args.parent_harness,
            "--parent-transport", args.parent_transport,
            "--parent-sandbox", args.parent_sandbox,
            "--child-harness", harness,
            "--launch-authority", args.launch_authority,
            "--worktree", args.worktree,
            "--json",
        ],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    )
    try:
        row = json.loads(result.stdout)
    except (ValueError, TypeError):
        return
    if (
        row.get("parent_harness") != args.parent_harness
        or row.get("parent_transport") != args.parent_transport
        or row.get("parent_sandbox") != args.parent_sandbox
        or row.get("child_harness") != harness
        or row.get("launch_authority") != args.launch_authority
        or row.get("status") not in ("supported", "unsupported", "unknown")
    ):
        return
    if row["status"] == "supported" and result.returncode != 0:
        # A failed probe process cannot mint launch-eligible evidence, even if
        # its stdout says supported; checked unsupported/unknown results keep
        # their nonzero-rc path and still fail closed downstream.
        return
    args.nested_eligibility = row["status"]
    args.eligibility_source = row.get("probe_source") or ""
    args.eligibility_failure_class = row.get("failure_class") or ""


def write_reset_cache(agent_home: Path, harness: str, reason: str, reset: str, jobs: Path | None = None) -> None:
    """SD-15↔SD-16: cache the last known limit reset for usage-check.sh to read.

    File `.dispatch/usage-reset.<harness>` holds one line: `<iso-ts> <reason> <reset>`.
    Best-effort — a cache write failure never blocks dispatch bookkeeping.
    """
    try:
        state_root = dispatch_state_root(jobs) if jobs else dispatch_state_roots(agent_home)[0]
        cache = state_root / f"usage-reset.{harness}"
        cache.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        cache.write_text(f"{ts} {reason} {reset}\n", encoding="utf-8")
    except (OSError, DispatchContractError):
        # Best effort: the reset cache is an observation, never a launch condition.
        pass


def diff_attribution_prompt(args: argparse.Namespace) -> str:
    """SD-156: `diff_base`/`pre_node_commits` lines for a node downstream of `execute`.

    One shared computation (`dispatch_contract.diff_attribution_lines`), added
    to the "Dispatch metadata:" block the three wrappers already assemble --
    the only place prompt text is composed for every registered launch surface
    (`stage-dispatch-fallback.py` forwards a prompt file built here, not its
    own).
    """
    route_file = getattr(args, "route_file", None) or getattr(
        getattr(args, "owner_route_binding", None), "route_file", None,
    )
    route_node = getattr(args, "route_node", None)
    if not route_file or not route_node:
        return ""
    try:
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    node = next((row for row in route.get("nodes", []) if row.get("id") == route_node), None)
    if node is None:
        return ""
    # The exact registry the launch was given or inherited; otherwise the
    # canonical one. A registry that cannot be resolved adds no lines.
    explicit_or_inherited_jobs = getattr(args, "jobs", None) or os.environ.get("AGENT_DISPATCH_JOBS", "")
    try:
        jobs = (Path(explicit_or_inherited_jobs) if explicit_or_inherited_jobs
                else resolve_dispatch_state_root(args.agent_home) / "jobs.log")
    except DispatchContractError:
        return ""
    lines = diff_attribution_lines(route, node, jobs)
    return "".join(f"- {line}\n" for line in lines)


# The completion deliveries whose owner a runtime supervisor holds under a lease.
SUPERVISED_DELIVERIES = frozenset({"session-resume-supervised", "app-server-supervised"})


def completion_lease_path(jobs: Path, args: argparse.Namespace) -> Path:
    if not getattr(args, "attempt_id", None):
        return dispatch_state_root(jobs) / "supervisor-state" / "preview-only.lease"
    return supervisor_lease_path(jobs, args.attempt_id)


def append_job(jobs: Path, args: argparse.Namespace, *, harness: str, runtime_sandbox: str,
               effort_key: str, claim, marker_gate, adapter_fields: str = "",
               replacement_sandbox: str | None = None) -> bool:
    """Register one attempt row; the same fields, order and claim for every harness.

    The wrapper passes what is its own: the harness name, how it names its
    sandbox and effort, any fields only it records, and -- looked up in its
    own module at call time -- the claim and completion-gate functions.
    """
    jobs.parent.mkdir(parents=True, exist_ok=True)
    repo = subprocess.check_output(["git", "-C", args.worktree, "rev-parse", "--show-toplevel"], text=True).strip()
    pipe = (
        f"capability={args.capability},capability_mode={args.capability_mode},qa={args.qa},"
        f"intensity={args.intensity},attempt_schema_version=2,"
        f"dispatch_depth={args.dispatch_depth},transport=headless,"
        f"execution_surface={args.execution_surface},"
        f"registered_worker={int(bool(args.registered_worker))},"
        f"fallback_hop={args.fallback_hop},harness={harness}"
    )
    if args.parent_slug:
        pipe += f",parent={args.parent_slug}"
    if getattr(args, "parent_binding", None) is not None:
        binding = args.parent_binding
        pipe += (
            f",parent_attempt_id={binding.attempt_id}"
            f",parent_pid={binding.pid},parent_pid_start={binding.pid_start}"
            f",parent_pid_scope={binding.pid_scope}"
            f",parent_liveness_source={binding.liveness_source}"
        )
        if binding.pid_host is not None:
            pipe += (
                f",parent_pid_host={binding.pid_host}"
                f",parent_pid_host_start={binding.pid_host_start}"
            )
    if args.parent_session_id:
        pipe += f",parent_sid={args.parent_session_id}"
    if args.parent_slug or args.parent_session_id:
        # OPERATIONS §5.10 pipe contract lists parent_cwd; without it a cross-harness
        # child whose parent_sid is synthetic can never nest in Fleet (2026-07-15).
        pipe += f",parent_cwd={parent_completion.effective_parent_cwd(args)}"
    if args.worker_role:
        pipe += f",worker_role={args.worker_role}"
    if args.worker_mode:
        pipe += f",worker_mode={args.worker_mode}"
    pipe += f",worker_type={args.worker_type},runtime_sandbox={runtime_sandbox}"
    pipe += execution_access_receipt_fragment(
        getattr(args, "execution_access_grant", None)
    )
    for key, value in sorted(args.launch_lifecycle_resolution.metadata().items()):
        pipe += f",{key}={value}"
    pipe += f",assigned_contract={args.assigned_contract}"
    if args.unit:
        pipe += f",unit={args.unit}"
    if args.review_output:
        binding = args.review_output_binding
        pipe += (
            f",review_cycle_id={binding['cycle_id']}"
            f",review_producer_id={binding['producer_id']}"
            f",review_output_locator_b64={binding['locator_b64']}"
            f",review_output_digest={binding['digest']}"
        )

    if args.capability_owner:
        pipe += f",owner={args.capability_owner}"
    if args.owner_harness:
        pipe += f",owner_harness={args.owner_harness}"
    if args.dispatch_depth >= 2:
        pipe += (
            f",parent_harness={args.parent_harness},parent_transport={args.parent_transport}"
            f",parent_sandbox={args.parent_sandbox},child_harness={harness}"
            f",nested_eligibility={args.nested_eligibility},eligibility_source={args.eligibility_source}"
            f",eligibility_failure_class={args.eligibility_failure_class or '-'}"
            f",eligibility_probe={getattr(args, 'eligibility_probe', None) or '-'}"
        )
    for key in ("route_file", "route_id", "route_hash", "route_node", "registry_digest", "write_scope", "completion_gate", "harness_affinity", "explicit_adapter"):
        value = getattr(args, key)
        if value:
            pipe += f",{key}={value}"
    if getattr(args, "route_validation", None):
        # SD-156: `route_validation` is worker-route-guard's own JSON, already
        # captured in `validate_route_record`. One helper merges its
        # `source_lineage` fields the same way for every registered launch.
        try:
            validation_json = json.loads(args.route_validation)
        except (TypeError, ValueError):
            validation_json = {}
        for key, value in sorted(source_lineage_row_fields(validation_json).items()):
            pipe += f",{key}={value}"
    if getattr(args, "owner_route_binding", None):
        pipe += (
            f",owner_route_file={args.owner_route_binding.route_file}"
            f",owner_route_id={args.owner_route_binding.route_id}"
            f",owner_route_hash={args.owner_route_binding.route_hash}"
        )
    pipe += workflow_completion_receipt(args)
    settings = args.resolved_model_settings
    for key, value in sorted(getattr(args, "profile_selection_receipt", {}).items()):
        pipe += f",{key}={value}"
    pipe += (
        f",model_source={settings['source']},model_role={settings['role']}"
        f",model_profile={settings['profile']},model_tier={settings['tier']}"
        f",profile_granularity={settings['granularity']}"
        f",model={settings['model']},{effort_key}={settings[effort_key]}"
        f",model_pin_status={settings.get('pin_status', 'none')}"
        + (f",model_pin={settings['pin_model']}" if settings.get("pin_model") else "")
    )
    pipe += adapter_fields
    pipe += (
        f",completion_delivery={getattr(args, 'resolved_completion_delivery', None) or '-'}"
        f",completion_delivery_reason={getattr(args, 'completion_delivery_reason', None) or 'not-applicable'}"
    )
    if getattr(args, "resolved_completion_delivery", None) in SUPERVISED_DELIVERIES:
        pipe += (
            f",supervisor_lease={SUPERVISOR_LEASE_KIND}"
            f",supervisor_lease_file={completion_lease_path(jobs, args)}"
            f",supervisor_lease_nonce={secrets.token_hex(32)}"
        )
    pipe += (
        f",parent_completion_delivery={args.parent_completion_delivery}"
        f",parent_completion_reason={getattr(args, 'parent_completion_reason', None) or 'unspecified'}"
    )
    if args.parent_completion_delivery == "claude-parent-runtime":
        # SD-111 P2 round 2 C-3 (2-a-5): the carrier-1 claim gate needs proof
        # that the hook process it eventually runs in descends from the same
        # runtime session this row's owner launched under. Ancestor-resolution
        # failure writes none of the three fields (partial recording would let
        # a hook falsely treat "unresolved" as "matches") -- carrier 1 then
        # fails closed on this row and completion still reaches the user via
        # exact harvest (§3.2.1 second fork).
        ancestry = runtime_ancestry_binding(os.getpid())
        if ancestry is not None:
            ancestry_pid, ancestry_start, ancestry_ns = ancestry
            pipe += (
                f",parent_runtime_pid={ancestry_pid}"
                f",parent_runtime_pid_start={ancestry_start}"
                f",parent_runtime_ns={ancestry_ns}"
            )
    if getattr(args, "profile", None):
        pipe += f",profile={args.profile}"
    # launch_home seals the resolved AGENT_HOME this wrapper launched under, so a
    # reader (fleet) can locate the default log dir without guessing the install
    # layout — the registry row may live in a different runtime home than the logs.
    pipe += (
        f",artifact_root={args.artifact_root},log_file={args.log_path}"
        f",launch_home={sealed_launch_home(args.agent_home)}"
    )
    pipe += stage_session_metadata(args)
    if args.attempt_id:
        pipe += (
            f",attempt_id={args.attempt_id},launch_authority={args.launch_authority}"
            f",fallback_ordinal={args.fallback_ordinal},launch_fence=registry-v1"
        )
    replica_reservation = getattr(args, "replica_batch_reservation", {})
    if replica_reservation:
        pipe += (
            f",parallel_group={replica_reservation['batch_group']}"
            f",replica_group={replica_reservation['batch_group']}"
        )
        for key in REPLICA_RESERVATION_ROW_KEYS:
            if key in replica_reservation:
                pipe += f",{key}={replica_reservation[key]}"
    leg_class, auxiliary_check = route_node_leg_fields(args)
    pipe += f",leg_class={leg_class},auxiliary_check={auxiliary_check}"
    if getattr(args, "automatic_retry_of", None):
        pipe += f",automatic_retry_of={args.automatic_retry_of}"
    if args.capacity_retry:
        pipe += (
            f",capacity_retry=1,prior_attempt_id={args.prior_attempt_id}"
            f",cooled_model={args.cooled_model},selection_source={args.selection_source}"
        )
    if args.broker_request_id:
        pipe += f",broker_request_id={args.broker_request_id}"
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    from review_input import registration_fragment
    pipe += commit_policy.registry_fragment(args)
    pipe += registration_fragment(args)
    from dispatch_replacement import seal_launch_input
    if replacement_sandbox is not None:
        args.replacement_runtime_sandbox = replacement_sandbox
    pipe += seal_launch_input(args, harness, getattr(args, "replacement_raw_task", ""))
    row = f"{ts}\topen\t{repo}\t{args.worktree}\t{args.slug}\t{pipe}"
    exclusive = ({"route_id": args.route_id, "route_node": args.route_node,
                  "capacity_retry": "1"} if args.capacity_retry else None)
    quick_exclusive = ({"route_id": args.route_id, "route_node": args.route_node}
                       if getattr(args, "quick_attempt", False) else None)
    preclaim = None
    if args.action == "start" and args.route_file:
        preclaim = lambda lines: marker_gate(
            args.route_file,
            args.route_node,
            args.action,
            args.agent_home,
            jobs,
            registry_lines=lines,
            attempt_id=args.attempt_id,
        )
    args.launch_preclaim = preclaim
    mutation_precheck = lambda lines: ensure_terminal_claim_absent(
        jobs, args.route_id, args.parent_attempt_id or args.attempt_id
    )
    return claim(
        jobs, args.attempt_id, row, launch=False,
        exclusive_metadata=exclusive,
        exclusive_live_metadata=quick_exclusive,
        terminal_attempt_limit=getattr(args, "quick_attempt_limit", None),
        replacement_attempt_limit=getattr(args, "replacement_attempt_limit", 0),
        replacement_notes=getattr(args, "replacement_notes", frozenset()),
        mutation_precheck=mutation_precheck,
        preclaim=preclaim,
    )
