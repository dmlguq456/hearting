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
import hashlib
import json
import os
import re
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
    REPLICA_RESERVATION_ROW_KEYS, STANDARD_PLUS_INTENSITIES, SUPERVISOR_LEASE_KIND,
    DispatchContractError, diff_attribution_lines,
    dispatch_state_root, dispatch_state_roots, resolve_dispatch_state_root, runtime_ancestry_binding,
    sealed_launch_home, source_lineage_row_fields, supervisor_lease_path, workflow_completion_receipt,
    ensure_terminal_claim_absent, _atomic_registry_replace, close_attempt_row,
    parse_registry_metadata,
    diagnostic_line,
)
import commit_policy
import dispatch_parent_completion as parent_completion
from dispatch_mode_contract import DispatchModeContractError, validate_route_mode_axes
from execution_access import receipt_fragment as execution_access_receipt_fragment
from stage_session_runtime import metadata as stage_session_metadata
from model_config import (
    ModelConfigError, headless_model_refusal, main_session_only_models,
    main_session_only_state, resolve_config, resolve_context_window, restricted_model,
)
from owner_route_binding import OwnerRouteBindingError, validate_runtime_requirements
import route_authority
from route_authority import scan_anchored_death

ROOT = Path(__file__).resolve().parents[1]


def reexec_for_sealed_release(wrapper_root: Path, sealed_home: Path, harness: str,
                              argv: list[str]) -> bool:
    """Re-run this wrapper from a sibling managed release matching its sealed home."""
    if harness not in {"claude", "codex", "opencode"}:
        return False
    try:
        wrapper_root = Path(wrapper_root).resolve(strict=True)
        sealed_root = Path(sealed_home).resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    if wrapper_root == sealed_root or wrapper_root.parent != sealed_root.parent:
        return False
    releases_root = wrapper_root.parent
    if releases_root.name != "releases" or not releases_root.is_dir():
        return False

    for release in (wrapper_root, sealed_root):
        marker = release / ".hearting-release.json"
        if marker.is_symlink() or not marker.is_file():
            return False

    target = sealed_root / "adapters" / harness / "bin" / "dispatch-headless.py"
    try:
        target = target.resolve(strict=True)
        if not target.is_relative_to(sealed_root) or not target.is_file():
            return False
    except (OSError, RuntimeError, TypeError, ValueError):
        return False

    # The next wrapper resolves AGENT_HOME to this same real root and therefore
    # passes the sibling check once. Keep argv as tokens and preserve the
    # interpreter's normal descriptor, signal, and exit behavior through exec.
    os.environ["AGENT_HOME"] = str(sealed_root)
    os.execv(sys.executable, [sys.executable, str(target), *argv])
    return True


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


def registered_owner(args: argparse.Namespace) -> bool:
    return args.dispatch_depth == 1 and args.worker_type == "owner"


def completion_owner(args: argparse.Namespace, *, registered=registered_owner) -> bool:
    return registered(args) and args.intensity in STANDARD_PLUS_INTENSITIES


def async_wait_policy(args: argparse.Namespace, *, supervised: bool, deny_tools) -> str:
    """The adapter declares its supervisor and proven tool names; policy lives here.

    Look up the deny tools only for an unsupervised turn, preserving the wrapper's
    call-time dependency and the supervised path's short circuit.
    """
    if supervised:
        return "runtime-supervised"
    return "deny-proven" if deny_tools(args) else "unsupported"


def completion_state_path(args: argparse.Namespace) -> Path:
    state_root = dispatch_state_root(args.jobs_path)
    if not args.attempt_id:
        return state_root / "supervisor-state" / "preview-only.json"
    attempt_id = args.attempt_id
    if re.fullmatch(r"att-[A-Za-z0-9._-]{1,240}", attempt_id) is None:
        raise DispatchContractError(
            "completion-state-attempt-invalid",
            "supervised completion requires a path-safe exact attempt id",
        )
    return state_root / "supervisor-state" / f"{attempt_id}.json"


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
    if args.action in {"register", "start"}:
        # Validate the complete shared projection before sealing permissions or
        # mutating the registry. Adapter settings and this row were rendered
        # from the captured projection above.
        commit_policy.assert_launch_permission_projection(args)
    jobs.parent.mkdir(parents=True, exist_ok=True)
    repo = getattr(args, "repository_identity", None)
    if not repo:
        from dispatch_contract import work_root_identity
        repo = work_root_identity(
            args.worktree,
            route_file=getattr(args, "route_file", None)
            or os.environ.get("AGENT_OWNER_ROUTE_FILE"),
        )
    pipe = (
        f"capability={args.capability},capability_mode={args.capability_mode},qa={args.qa},"
        f"intensity={args.intensity},attempt_schema_version=2,"
        f"dispatch_depth={args.dispatch_depth},transport=headless,"
        f"execution_surface={args.execution_surface},"
        + ("repository=non-git," if getattr(args, "repository_kind", "git") == "non-git" else "")
        + f"registered_worker={int(bool(args.registered_worker))},"
        + f"fallback_hop={args.fallback_hop},harness={harness}"
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
    main_identity = _retention_main_identity(jobs, args, harness)
    if main_identity is not None:
        main_harness, main_pid, main_start, main_ns = main_identity
        pipe += (f",main_runtime_harness={main_harness},main_runtime_pid={main_pid}"
                 f",main_runtime_pid_start={main_start},main_runtime_ns={main_ns}")
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
    if args.route_file and args.route_node and args.attempt_id:
        from review_source_subject import SourceSubjectError, has_review_subject, observe
        try:
            route = json.loads(Path(args.route_file).read_text(encoding="utf-8"))
            node = next((item for item in route.get("nodes", [])
                         if item.get("id") == args.route_node), None)
            if node is not None and has_review_subject(route, node, jobs):
                observed = observe(route, node, worktree=args.worktree, jobs=jobs)
                expected = os.environ.get("AGENT_REVIEW_SOURCE_SUBJECT_DIGEST")
                if expected and observed["digest"] != expected:
                    raise SourceSubjectError("review-source-admission-subject-changed", args.attempt_id)
        except SourceSubjectError as exc:
            raise DispatchContractError(exc.reason, exc.detail) from exc
        except (OSError, ValueError, StopIteration) as exc:
            raise DispatchContractError("review-source-route-unavailable", str(exc)) from exc
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
    if args.action == "start":
        def preclaim(lines):
            # This is the last existing safety fence before spawn. Reobserve
            # Git here so a state transition after registration cannot launch
            # with native settings that no longer match the current decision.
            commit_policy.assert_launch_permission_projection(args)
            if args.route_file:
                marker_gate(
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


def _interactive_main_identity(jobs: Path, args, harness: str):
    """Resolve an exact interactive runtime ancestor for a depth-one launch."""
    sid = str(getattr(args, "parent_session_id", "") or "")
    if getattr(args, "dispatch_depth", 0) != 1 or not sid:
        return None
    try:
        sys.path.insert(0, str(ROOT / "utilities"))
        from session_identity import session_ids
        if session_ids(os.environ).get(harness, (None,))[0] != sid:
            return None
        if harness == "claude":
            identity = runtime_ancestry_binding(os.getpid())
            if identity is None:
                return None
            pid, start, namespace = identity
            from fleet.collectors import claude
            row = claude.read_registry(int(pid)) or {}
            if (str(row.get("procStart") or "") != start
                    or str(row.get("sessionId") or "") != sid):
                return None
            return harness, pid, start, namespace
        if harness == "codex":
            from pane_ownership import codex_caller_binding
            binding = codex_caller_binding(sid)
            if binding is None:
                return None
            pid, start = str(binding["pid"]), str(binding["pid_start"])
            namespace = process_namespace_identity(int(pid))
            return (harness, pid, start, namespace) if namespace else None
        if harness == "opencode":
            from fleet.process_identity import PROVEN, _proc_stat, process_identity
            current, seen = os.getpid(), set()
            for _ in range(128):
                if current <= 1 or current in seen:
                    break
                seen.add(current)
                found = process_identity(current, "opencode")
                if (found.confidence == PROVEN and found.session_id == sid
                        and found.source == "opencode-tui-selection"):
                    parent, start = _proc_stat(current)
                    namespace = process_namespace_identity(current)
                    if parent is not None and start and process_start_ticks(current) == start and namespace:
                        return harness, str(current), start, namespace
                parent, _start = _proc_stat(current)
                if parent is None:
                    break
                current = parent
    except Exception:
        return None
    return None


def _retention_main_identity(jobs: Path, args, harness: str):
    """Record a depth-one identity or inherit it through explicit attempt ancestry."""
    depth = getattr(args, "dispatch_depth", 0)
    if depth == 1:
        return _interactive_main_identity(jobs, args, harness)
    if depth < 2:
        return None
    binding = getattr(args, "parent_binding", None)
    attempt = str(getattr(binding, "attempt_id", "") or "")
    if not attempt:
        return None
    try:
        latest = {}
        for line in Path(jobs).read_text(encoding="utf-8", errors="replace").splitlines():
            fields = line.split("\t")
            if len(fields) != 6:
                continue
            row = parse_registry_metadata(fields[5])
            if row.get("attempt_id"):
                latest[row["attempt_id"]] = row
        seen = set()
        for _ in range(32):
            if attempt in seen:
                return None
            seen.add(attempt)
            row = latest.get(attempt)
            if row is None:
                return None
            names = ("main_runtime_harness", "main_runtime_pid", "main_runtime_pid_start",
                     "main_runtime_ns")
            if all(row.get(name) for name in names):
                return tuple(row[name] for name in names)
            attempt = row.get("parent_attempt_id", "")
            if not attempt:
                return None
    except (OSError, ValueError):
        return None
    return None


def close_job_row(jobs: Path, slug: str, worktree: str, reason: str, reset: str,
                  attempt_id: str | None = None, *, materialize) -> bool:
    """Flip this dispatch's own open row to done with a dead-<reason> note.

    The wrapper passes what is its own: looked up in its own module at call
    time, the terminal-close materialization its tests patch in place.
    """
    if attempt_id:
        evidence = {"reset": reset} if reset else {}
        if reason == "capacity":
            evidence.update(failure_class="capacity", detected_by="anchored-early-exit")
        closed = close_attempt_row(jobs, attempt_id, f"dead-{reason}", evidence=evidence)
        if closed:
            materialize(jobs, attempt_id)
        return closed
    if not jobs.is_file():
        return False
    with jobs_lock(jobs):
        lines = jobs.read_text(encoding="utf-8").splitlines(keepends=True)
        changed = False
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 6:
                continue
            ts, status, repo, wt, row_slug, pipe = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]
            if status != "open" or row_slug != slug or wt != worktree:
                continue
            metadata = parse_registry_metadata(pipe)
            if metadata.get("attempt_schema_version") != "2":
                continue
            if attempt_id and f"attempt_id={attempt_id}" not in pipe.split(","):
                continue
            pipe += f",note=dead-{reason}"
            if reason == "capacity":
                pipe += ",failure_class=capacity,detected_by=anchored-early-exit"
            if reset:
                pipe += f",reset={reset}"
            lines[i] = f"{ts}\tdone\t{repo}\t{wt}\t{row_slug}\t{pipe}\n"
            changed = True
            break
        if changed:
            # Lock-free readers (join, owner input) must never see a truncated registry.
            _atomic_registry_replace(jobs, "".join(lines).splitlines())
        return changed


def attach_summary_owner(args, log_path: Path, prompt_path: Path, identity, *,
                         harness: str, summary_launcher, runtime_home: str | None = None):
    """Attach the summary owner; the wrapper passes its harness name and its own
    `launch_summary_owner`, looked up in its module at call time so tests that
    patch the wrapper's name keep working."""
    review = args.review_output_binding
    return summary_launcher(
        attempt_id=args.attempt_id,
        harness=harness,
        transcript=log_path,
        prompt_path=prompt_path,
        target_pid=int(identity["pid"]),
        target_start=identity["pid_start"],
        runtime_home=runtime_home,
        attempt_cwd=getattr(args, "worktree", None),
        review_artifact_root=review["artifact_root"] if review else None,
        review_cycle_id=review["cycle_id"] if review else None,
        review_lease_nonce=args.review_governed_lease_nonce if review else None,
    )


def validate_route_record(args: argparse.Namespace, *, marker_gate) -> int:
    """One admission check for every wrapper.

    The wrapper passes its own completion-marker gate, looked up in its own
    module at call time (the sd45 and review_input tests patch that name).
    """
    routed = any((args.route_id, args.route_hash, args.route_node, args.registry_digest))
    if routed and not args.route_file:
        return fail("route-record-required", 65, route_id=args.route_id or "-")
    if not args.route_file:
        return 0
    required = ("route_id", "route_hash", "route_node", "registry_digest", "write_scope")
    missing = [name for name in required if not getattr(args, name)]
    if missing:
        return fail("route-metadata-missing", 65, fields=",".join(missing))
    try:
        route_record = json.loads(Path(args.route_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        route_record = {}
    if route_record.get("schema_version") != 2 or "broker_contract_version" in route_record:
        return fail("legacy-broker-route-read-only", 65, route_file=args.route_file, child_spawned="0")
    try:
        validate_runtime_requirements(route_record, args.route_node)
    except OwnerRouteBindingError as exc:
        return fail(str(exc), 69, child_spawned="0", fallback="inline-or-main")
    try:
        validate_route_mode_axes(args, route_record)
    except DispatchModeContractError as exc:
        return fail(exc.reason, 65, **exc.fields, child_spawned="0")
    command = [sys.executable, str(ROOT / "utilities" / "worker-route-guard.py"), "validate",
        "--route", args.route_file, "--node", args.route_node, "--cwd", args.worktree,
        "--artifact-root", args.artifact_root, "--capability", args.capability,
        "--intensity", args.intensity, "--write-scope", args.write_scope,
        "--route-id", args.route_id, "--route-hash", args.route_hash,
        "--registry-digest", args.registry_digest,
        "--unit", args.unit,
        "--launch-phase", args.action,
        "--model-role", args.model_role or "",
        "--model-profile", args.model_profile or ""]
    if args.attempt_id:
        command += ["--current-attempt", args.attempt_id]
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        if result.stdout: print(result.stdout, end="")
        if result.stderr: print(result.stderr, end="", file=sys.stderr)
        reason, detail = _route_guard_refusal(
            result.stderr, returncode=result.returncode,
            route_id=args.route_id, route_file=args.route_file)
        return fail(
            reason, result.returncode,
            detail=detail,
            route_file=args.route_file, registered="0", started="0",
            child_spawned="0",
        )
    args.route_validation = result.stdout.strip()
    # Run the dependency gate before runtime-projection and parent-registry
    # diagnostics so an invalid DAG edge is the stable first failure. The
    # authoritative registry path is revalidated immediately before claim.
    refusal = route_authority.completion_gate(
        args, args.action, args.agent_home, route_authority.prelaunch_registry(args), gate=marker_gate)
    if refusal:
        reason, code, fields = refusal
        return fail(reason, code, **fields)
    return 0


def prepare_pair_model_rotation(args: argparse.Namespace, *, harness: str) -> dict[str, object]:
    """Re-derive a narrow pair hint from the existing route and pair evidence.

    The caller's hint is never evidence. An unverifiable hint is discarded and
    the sealed profile remains the launch profile.
    """
    sealed = getattr(args, "model_profile", None)
    context: dict[str, object] = {
        "sealed_profile": sealed or "", "profile": sealed or "",
        "status": "none", "sibling_model": "-",
    }
    hint = getattr(args, "pair_model_profile", None)
    if not sealed or getattr(args, "model", None):
        if getattr(args, "model", None):
            context["status"] = "explicit-model"
        args.pair_model_context = context
        return context
    route_path = getattr(args, "route_file", None) or getattr(
        getattr(args, "owner_route_binding", None), "route_file", None)
    if not route_path:
        context["status"] = "none"
        args.pair_model_context = context
        return context
    try:
        from model_config import resolve_config
        from model_profile import pair_model_rotation, resolve_profile_values
        route = json.loads(Path(route_path).read_text(encoding="utf-8"))
        nodes = route.get("nodes", [])
        node_id = getattr(args, "route_node", None)
        current = next(node for node in nodes if node.get("id") == node_id)
        pins = route_authority.selection_pin_rows(route)
        target_pin = pins.get("frame" if current.get("worker_type") == "frame"
                              or current.get("kind") == "frame" else "worker")
        if isinstance(target_pin, dict) and target_pin.get("model"):
            context["status"] = "explicit-model"
            args.pair_model_context = context
            return context
        group_key = "parallel_group" if current.get("parallel_group") else "replica_group"
        group = current.get(group_key)
        sibling = None
        if group:
            members = [node for node in nodes if node.get(group_key) == group]
            if len(members) != 2:
                raise ValueError("pair-cardinality-mismatch")
            token = os.environ.get("AGENT_MODEL_GOVERNOR_RESERVATION_TOKEN", "")
            if not token:
                raise ValueError("pair-reservation-missing")
            from dispatch_contract import (
                _governor_json, replica_batch_expectation,
                resolve_model_governor_root,
            )
            from replica_batch_contract import verify_manifest
            expected = replica_batch_expectation(
                route_path, node_id, getattr(args, "action", ""),
                attempt_id=getattr(args, "attempt_id", ""),
                parent_attempt_id=getattr(args, "parent_attempt_id", ""),
                harness=harness,
                fallback_hop=getattr(args, "fallback_hop", ""),
                fallback_ordinal=getattr(args, "fallback_ordinal", None),
                assignment_sha256=("sha256:" + hashlib.sha256(
                    str(getattr(args, "prompt_text", "")).encode("utf-8")).hexdigest()
                    if isinstance(getattr(args, "prompt_text", None), str) else ""),
            )
            if expected is None:
                raise ValueError("pair-reservation-not-applicable")
            governor_root = resolve_model_governor_root(args.artifact_root)
            governor = Path(args.agent_home) / "utilities" / "model-worker-governor.py"
            payload = _governor_json([
                sys.executable, str(governor), "--root", str(governor_root),
                "reservation-check", "--token", token, "--class", "dispatch",
            ], allow_absent=True)
            if payload.get("state") != "unclaimed":
                raise ValueError("pair-reservation-not-unclaimed")
            route_authority.require_batch_reservation(payload, expected)
            manifest, _, _ = verify_manifest(payload.get("batch_manifest"))
            manifest_members = manifest.get("members")
            if not isinstance(manifest_members, list) or len(manifest_members) != 2:
                raise ValueError("pair-manifest-cardinality-mismatch")
            own_members = [member for member in manifest_members
                           if member.get("route_node") == node_id]
            if len(own_members) != 1:
                raise ValueError("pair-manifest-current-member-mismatch")
            sibling_members = [member for member in manifest_members
                               if member.get("route_node") != node_id]
            if len(sibling_members) != 1:
                raise ValueError("pair-manifest-sibling-member-mismatch")
            sibling_member = sibling_members[0]
            if sibling_member.get("harness") != harness:
                raise ValueError("pair-sibling-harness-mismatch")
            sibling_id = str(sibling_member.get("route_node", ""))
            sibling = next(node for node in members if node.get("id") == sibling_id)
            if sibling.get("model_profile") != next(
                member.get("model_profile") for member in manifest_members
                if member.get("route_node") == sibling_id):
                raise ValueError("pair-sealed-profile-mismatch")
        else:
            if node_id != "frame-alternative":
                context["status"] = "none"
                args.pair_model_context = context
                return context
            if getattr(args, "worker_type", None) != "frame":
                raise ValueError("pair-route-node-unbound")
            jobs = (Path(args.jobs) if getattr(args, "jobs", None)
                    else resolve_dispatch_state_root(args.agent_home) / "jobs.log")
            rows = []
            for line in jobs.read_text(encoding="utf-8").splitlines():
                fields = line.split("\t")
                if len(fields) != 6:
                    continue
                meta = parse_registry_metadata(fields[5])
                if (meta.get("route_id") == route.get("route_id")
                        and meta.get("route_hash") == route.get("route_hash")
                        and meta.get("route_node") == "frame"
                        and meta.get("worker_type") == "frame"
                        and meta.get("harness") == harness and meta.get("model")):
                    rows.append(meta)
            if len(rows) != 1:
                raise ValueError("frame-sibling-model-ambiguous")
            context["sibling_model"] = str(rows[0]["model"])
        if sibling is not None:
            if sibling.get("model_profile") is None or sibling.get("model_profile") == "top":
                raise ValueError("pair-sibling-profile-unavailable")
            config, _ = resolve_config(harness, source_root=ROOT)
            sibling_values = resolve_profile_values(
                harness, config, str(sibling["model_profile"]))
            context["sibling_model"] = sibling_values["model"]
        config, _ = resolve_config(harness, source_root=ROOT)
        own_model = resolve_profile_values(harness, config, str(sealed))["model"]
        if own_model == context["sibling_model"]:
            pass
        else:
            context["status"] = "distinct"
            args.pair_model_context = context
            return context
        candidate = pair_model_rotation(harness, config, str(sealed), str(context["sibling_model"]))
        if candidate is None:
            context["status"] = "unavailable"
            args.pair_model_context = context
            return context
        if not hint or candidate["profile"] != hint:
            raise ValueError("pair-hint-does-not-match-shared-policy")
        from dispatch_capacity_evidence import usage_states
        jobs = (Path(args.jobs) if getattr(args, "jobs", None)
                else resolve_dispatch_state_root(args.agent_home) / "jobs.log")
        state = usage_states(jobs, profile=hint, models={harness: candidate["model"]}, recheck=False).get(harness, "unknown")
        if str(state).startswith("limited("):
            context["status"] = "target-limited"
            raise ValueError("pair-target-limited")
        context.update({"profile": hint, "status": "rotated", "sibling_model": candidate["model"]
                        if candidate["model"] == context["sibling_model"] else context["sibling_model"]})
    except Exception as exc:
        context["profile"] = sealed or ""
        if context.get("status") != "target-limited":
            context["status"] = "unverified-kept-sealed"
        context["detail"] = str(exc)[:200]
    args.pair_model_context = context
    return context


def _route_guard_refusal(stderr: str, *, returncode: int = 65,
                         route_id: str | None = None,
                         route_file: str | None = None) -> tuple[str, str]:
    """Carry a bounded, typed guard refusal across the common wrapper boundary."""
    generic = ("worker-route-validation-failed", "guard output lacked a typed refusal")
    if returncode != 65 or not isinstance(stderr, str) or len(stderr) > 16384:
        return generic
    try:
        row = json.loads(stderr)
    except (TypeError, ValueError):
        return generic
    if isinstance(row, dict) and row.get("status") == "blocked":
        # Older guards omit identity; the Git probe can fail before it knows
        # the route. Neither case creates a new required input.
        if route_id and row.get("route_id") not in (None, "unknown", route_id):
            return generic
        if route_file and row.get("route_file") not in (None, route_file):
            return generic
        reason, detail = row.get("reason"), row.get("detail")
        if (isinstance(reason, str) and 1 <= len(reason) <= 128
                and re.fullmatch(r"[a-z][a-z0-9-]*", reason)
                and isinstance(detail, str)):
            return reason, diagnostic_line(detail)
    return generic


def resolve_parent_completion_delivery(args: argparse.Namespace, *, probe) -> str:
    """The delivery this launch's parent will use; the wrapper passes its own
    `probe_managed_codex_parent`, looked up in its module at call time."""
    return parent_completion.resolve_parent_completion_delivery(args, probe=probe)


def bind_parent_completion_delivery(args: argparse.Namespace, *, probe) -> None:
    args.parent_completion_delivery = resolve_parent_completion_delivery(args, probe=probe)


def launch_parent_completion_sidecar(args: argparse.Namespace, jobs: Path, *, launch, annotate) -> None:
    parent_completion.launch_parent_completion_sidecar(args, jobs, launch=launch, annotate=annotate)


def acquire_review_lease_after_claim(
    args, jobs: Path, identity: dict[str, str], *,
    detached, lease_held, acquire_admission, acquire_foreground_admission, lease_acquire,
) -> dict[str, str]:
    """The review lease for a just-claimed attempt; the wrapper passes its own
    lifecycle constant and admission/probe/acquire functions, looked up in its
    module at call time so the reviewer_report_binding mocks keep working."""
    if not args.review_output:
        return {}
    binding = args.review_output_binding
    budget = getattr(args, "watchdog_budget", None)
    if budget is None:
        raise ProducerError("review-watchdog-budget-missing")
    witness_metadata = {
        "attempt_id": args.attempt_id,
        "review_cycle_id": binding["cycle_id"],
        "review_governed_lease": "summary-flock-v1",
        "review_governed_lease_nonce": args.review_governed_lease_nonce,
    }
    witness_unlocked = lambda: not lease_held(
        Path(args.artifact_root), witness_metadata
    )
    if args.launch_lifecycle == detached:
        handle = getattr(args, "review_watchdog_handle", None)
        if handle is None:
            raise ProducerError("review-watchdog-handle-missing")
        return acquire_admission(
            handle=handle, budget=budget, identity=identity,
            root=Path(args.artifact_root), cycle_id=binding["cycle_id"],
            attempt_id=args.attempt_id, review_output=binding["output_path"],
            binding=binding, jobs=jobs, nonce=args.review_governed_lease_nonce,
            lease_acquire=lease_acquire, witness_probe=witness_unlocked,
        )
    return acquire_foreground_admission(
        budget=budget, identity=identity, root=Path(args.artifact_root),
        cycle_id=binding["cycle_id"], attempt_id=args.attempt_id,
        review_output=binding["output_path"], binding=binding, jobs=jobs,
        lease_acquire=lease_acquire, witness_probe=witness_unlocked,
    )


def model_policy(harness: str, *, error) -> dict[str, str]:
    """This launch's model policy; the wrapper passes its own harness name and
    its `ModelSelectionError` class, so each adapter keeps raising its own."""
    try:
        values, _receipt = resolve_config(harness, source_root=ROOT)
    except ModelConfigError as exc:
        raise error("dispatch-model-policy-unavailable", str(exc)) from exc
    return values


def attach_context_window(settings: dict[str, str], harness: str, *, error) -> dict[str, str]:
    """Attach the common requested budget after any model-selection branch."""
    try:
        values, _receipt = resolve_config(harness, source_root=ROOT)
        window, provenance = resolve_context_window(values)
    except ModelConfigError as exc:
        raise error("dispatch-model-policy-unavailable", str(exc)) from exc
    settings["context_window"] = window
    settings["context_window_source"] = provenance
    return settings


def require_headless_model(model: str, source: str, *, policy, error) -> None:
    """Refuse a main-session-only model on a headless launch. The wrapper passes
    its own `_model_policy` looked up at call time (the eligibility tests patch
    the wrapper's name) and its `ModelSelectionError` class."""
    refusal = headless_model_refusal(policy(), model, source)
    if refusal:
        raise error(*refusal)


def main_session_only_model(model: str, policy) -> bool:
    """Whether the selected model is restricted to the main session. The
    wrapper passes its own `_model_policy` looked up at call time (the
    eligibility tests patch the wrapper's name)."""
    return restricted_model(model, main_session_only_models(policy()))


def main_session_only_policy_state(policy, *, error) -> str:
    """`declared`/`absent`/`unavailable` for the receipt. The wrapper passes its
    own `_model_policy` and its `ModelSelectionError` class, so each adapter
    keeps catching its own class."""
    try:
        return main_session_only_state(policy())
    except error:
        return "unavailable"
