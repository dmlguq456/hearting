#!/usr/bin/env python3
"""Register and supervise one serial execution chain beneath a route stage."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
from stage_session_contract import StageSessionError, load_manifest, sealed_pointer_bytes  # noqa: E402
from dispatch_contract import (  # noqa: E402
    DispatchContractError,
    GOVERNOR_RESERVATION_ENV,
    close_refused_chain_rows,
    probe_owner_supervision,
    SupervisionProbe,
    resolve_global_registry,
    resolve_model_governor_root,
)
import subdivision_batch_admission as SUBDIVISION_ADMISSION  # noqa: E402
import dispatch_subsession_resume_record as RESUME_RECORD  # noqa: E402
import parent_next_directive  # noqa: E402

_BATCH_SPEC = importlib.util.spec_from_file_location(
    "dispatch_batch_for_stage_session_chain", ROOT / "utilities" / "dispatch-batch.py"
)
if _BATCH_SPEC is None or _BATCH_SPEC.loader is None:
    raise ImportError("dispatch-batch.py could not be loaded")
DISPATCH_BATCH = importlib.util.module_from_spec(_BATCH_SPEC)
_BATCH_SPEC.loader.exec_module(DISPATCH_BATCH)  # type: ignore[union-attr]


def _resume_state_root(jobs: Path) -> Path:
    """The census reader and `dispatch_subsession_advance.record_owner_resume_
    if_chain()` (the writer) must derive the same state root from the same
    registry path, or the reader silently reads an empty ledger. Both use the
    canonical `<registry>/../` derivation with no extra normalization."""

    return Path(jobs).parent


def chain_census(jobs: Path, chain_id: str, session_count: int | None = None) -> dict[str, object]:
    """A-4 (F-2, impl-review round 2): the ONE production derivation of
    `runtime_joins`. It counts unique `subsession_owner_resume_v1` delivery
    ids and nothing else -- never a session count, never a hardcoded
    constant, never a supervisor-local variable. `subsession_advances` is the
    committed `ssadv-*` identity count for the same chain.

    Pre-execution both are 0; that is a measurement, not a declaration.
    """

    # Imported lazily: `dispatch_subsession_advance` loads THIS module through
    # importlib at advance time, so a module-level import here would make the
    # import order depend on which of the two a process reached first.
    import dispatch_subsession_advance as SUBSESSION_ADVANCE

    runtime_joins = RESUME_RECORD.unique_delivery_ids(_resume_state_root(jobs), chain_id)
    advances = SUBSESSION_ADVANCE.subsession_advances(Path(jobs), chain_id)
    census: dict[str, object] = {
        "runtime_joins": runtime_joins,
        "runtime_joins_source": RESUME_RECORD.EVENT_TYPE,
        "subsession_advances": advances,
    }
    if session_count is not None:
        census["expected_subsession_advances"] = max(0, session_count - 1)
    return census


def continuation_metrics(session_count: int) -> dict[str, int]:
    """`check`'s projected (pre-execution) join comparison. It reports the
    un-chained baseline only and deliberately emits NO `runtime_joins`: that
    value is a measurement, and the one surface that produces it is
    `chain_census()` reading the owner-resume delivery ledger (F-2).
    """
    return {
        "baseline_runtime_joins": session_count,
        "continuation_reduction": max(0, session_count - 1),
    }


def node_for(route: dict, node_id: str) -> dict:
    found = [item for item in route.get("nodes", []) if item.get("id") == node_id]
    if len(found) != 1:
        raise StageSessionError("route-node-not-unique")
    return found[0]


dispatch_command = SUBDIVISION_ADMISSION.dispatch_command


def run_checked(command: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True, check=False)


def parse_start_receipt(returncode: int, stdout: str, attempt_id: str) -> dict:
    """Accept a start only when the complete typed receipt proves spawning."""

    values: dict[str, str] = {}
    conflicts: set[str] = set()
    keys = {"check", "attempt_id", "registered", "started", "duplicate_attempt", "child_spawned", "reason"}
    for line in stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key not in keys:
            continue
        if key in values and values[key] != value:
            conflicts.add(key)
        values[key] = value
    wrapper_reason = values.get("reason", "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9:._-]{0,127}", wrapper_reason):
        wrapper_reason = ""
    if returncode != 0:
        verdict = "returncode-nonzero"
    elif conflicts:
        verdict = "receipt-field-conflict:" + sorted(conflicts)[0]
    else:
        for key in ("check", "attempt_id", "registered", "started", "duplicate_attempt", "child_spawned"):
            if key not in values:
                verdict = "receipt-field-missing:" + key
                break
        else:
            checks = (
                (values["check"] == "ok", "check-not-ok"),
                (values["attempt_id"] == attempt_id, "attempt-mismatch"),
                (values["duplicate_attempt"] == "0", "duplicate-attempt"),
                (values["registered"] == "1", "not-registered"),
                (values["started"] == "1", "not-started"),
                (values["child_spawned"] == "1", "not-spawned"),
            )
            verdict = next((reason for ok, reason in checks if not ok), "ok")
    if verdict != "ok" and not wrapper_reason:
        # Only a failed start whose own reason could not be read gets the placeholder.
        wrapper_reason = "invalid-wrapper-receipt"
    return {
        "ok": verdict == "ok", "verdict": verdict, "wrapper_reason": wrapper_reason,
        "returncode": returncode, "fields": values,
    }


def _supervision_refusal_reason(state: str) -> str:
    return (
        "subsession-chain-advance-unsupervised"
        if state == "unsupervised"
        else "subsession-chain-advance-supervision-unproven"
    )


def _print_refusal_tail(closed, *, include_fallback: bool = True, include_counts: bool = True) -> None:
    """Print the common refusal tail, including the exact parent directive."""

    if include_fallback:
        print(f"fallback={'single-session-after-delivery' if closed.unclosed else 'single-session-required'}")
    if include_counts:
        print(f"cancelled_rows={len(closed.cancelled)}")
        print(f"already_closed_rows={len(closed.already_closed)}")
        print(f"unclosed_rows={len(closed.unclosed)}")
        print(f"unclosed_attempt_ids={','.join(closed.unclosed) or 'none'}")
    if not closed.unclosed:
        return
    selected = 0
    for index, attempt_id in enumerate(closed.unclosed):
        delivery = closed.unclosed_delivery[index] if index < len(closed.unclosed_delivery) else ""
        next_action, _, _ = parent_next_directive.parent_next(
            delivery, attempt_id, agent_home=ROOT
        )
        if next_action != parent_next_directive.NEXT_END_TURN:
            selected = index
            break
    attempt_id = closed.unclosed[selected]
    delivery = closed.unclosed_delivery[selected] if selected < len(closed.unclosed_delivery) else ""
    for line in parent_next_directive.receipt_lines(delivery, attempt_id, agent_home=ROOT):
        print(line)


def chain_manifest_pointer_path(jobs: Path, chain_id: str) -> Path:
    """Canonical, chain_id-keyed manifest pointer -- the only durable location
    a later, unrelated process (a session supervisor advancing this chain,
    SD-119 R2) can find the sealed manifest from, since the original
    `--manifest` envelope path is caller-local and not otherwise discoverable
    from a registry row alone."""

    return jobs.parent / "session_chains" / f"{chain_id}.json"


def persist_chain_manifest(jobs: Path, manifest: dict) -> None:
    path = chain_manifest_pointer_path(jobs, manifest["chain_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(sealed_pointer_bytes(manifest))


LAUNCH_PHASE_BY_ACTION = {
    "check": "dry-run",
    "census": "dry-run",
    "register": "register",
    "start": "start",
}


def _run_parallel_subdivision(
    route_record: dict, node: dict, args: argparse.Namespace, *, jobs: Path
) -> int:
    """SD-119 R4: `mode == "parallel"` routes to the dedicated sub-session batch
    admission surface instead of raising `parallel-subsession-use-dispatch-batch`
    -- that redirect described a mechanism (route-leg `dispatch-batch` groups)
    this manifest was never eligible for (SD-119 (1)); the fix is to route to
    the surface that is actually reachable, not to keep the dead-end typed."""

    agent_home = ROOT
    artifact_root = Path(
        os.environ.get("AGENT_ARTIFACT_ROOT", str(agent_home / ".agent_reports"))
    )
    governor = ROOT / "utilities" / "model-worker-governor.py"
    governor_root = resolve_model_governor_root(artifact_root)
    try:
        # F-3 narrowed by SD-103: only a non-worktree-base slice is refused
        # here -- see `raise_if_parallel_entry_fail_closed` in
        # subdivision_batch_admission.py.
        SUBDIVISION_ADMISSION.raise_if_parallel_entry_fail_closed(args.manifest)
        admission = SUBDIVISION_ADMISSION.admit_batch(
            route=route_record, node=node, manifest_path=args.manifest,
            governor=governor, governor_root=governor_root,
            reserve=DISPATCH_BATCH.reserve_plain_slots, jobs=jobs,
        )
    except SUBDIVISION_ADMISSION.SubdivisionAdmissionError as exc:
        print(json.dumps({
            "schema_version": 1, "state": "subdivision-batch-refused",
            "chain_id": None, "reason": exc.reason, "detail": exc.detail,
            "admitted_rows": 0, "admitted_models": 0,
        }, sort_keys=True))
        return 65
    if args.action == "register":
        print(f"chain_id={admission.manifest['chain_id']}")
        print(f"registered_sessions={len(admission.sessions)}")
        return 0
    # The batch helper can only precheck before crossing into the adapter
    # subprocess; the adapter's jobs.log.lock critical section is the fence.
    results = SUBDIVISION_ADMISSION.start_admitted_batch(
        admission, parent=args.parent, jobs=jobs,
        governor_reservation_env=GOVERNOR_RESERVATION_ENV,
    )
    if any(row.get("refusal_reason") for row in results):
        # F-4: an incomplete registration is a batch refusal, not a partial
        # success with some counters at 0 -- it prints the same typed refusal
        # envelope the admission-gate failure above prints.
        print(json.dumps({
            "schema_version": 1, "state": "subdivision-batch-refused",
            "chain_id": admission.manifest["chain_id"],
            "reason": SUBDIVISION_ADMISSION.BATCH_REGISTRATION_INCOMPLETE,
            "admitted_rows": 0, "admitted_models": 0,
            "cancelled_rows": sum(int(row.get("cancelled") or 0) for row in results),
        }, sort_keys=True))
        return 65
    print(f"chain_id={admission.manifest['chain_id']}")
    print(f"chain_manifest_sha256={admission.manifest_digest}")
    print(f"registered_sessions={len(admission.sessions)}")
    print(f"registered={sum(1 for row in results if row.get('registered'))}")
    print(f"started={sum(1 for row in results if row.get('started'))}")
    print(f"child_spawned={sum(1 for row in results if row.get('started'))}")
    print("runtime_wait=registered-children")
    return 0 if all(row.get("started") for row in results) else 1



# SD-103 cheap path: `plan_slices` lives in `subdivision_batch_admission` (both
# entry points share it); re-exported here for the CLI and its tests.
plan_slices = SUBDIVISION_ADMISSION.plan_slices


def single_session_next_action(route_file, node_id: str, slug: str, parent: str) -> str:
    return SUBDIVISION_ADMISSION.single_session_next_action(route_file, node_id, slug, parent)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("check", "census", "register", "start", "plan-slices"))
    p.add_argument("--manifest", help="chain manifest (check/census/register/start)")
    p.add_argument("--parent", help="owner slug (register/start); default: this session's own (AGENT_DISPATCH_SELF_SLUG)")
    p.add_argument("--jobs")
    p.add_argument("--route", help="plan-slices: compiled route file")
    p.add_argument("--node", default="execute", help="plan-slices: subdivision-permitted node id")
    p.add_argument("--worktree", help="plan-slices: the route's sealed cwd or a linked worktree of the same repository (default: the route's cwd)")
    p.add_argument("--slices", help="plan-slices: plan .md with one `slices` block, or a JSON slice list")
    p.add_argument("--output", help="plan-slices: manifest path to write (briefs are written beside it)")
    p.add_argument("--adapter", default=None, choices=("claude", "codex", "opencode"))
    args = p.parse_args()
    from dispatch_terminal_commit import require_current_cleanup
    require_current_cleanup('chain')
    if args.action == "plan-slices":
        missing = [name for name in ("route", "slices", "output") if not getattr(args, name)]
        if missing:
            p.error("plan-slices requires --" + ", --".join(missing))
        try:
            print(json.dumps(plan_slices(
                route_path=Path(args.route), node_id=args.node,
                worktree=Path(args.worktree) if args.worktree else None,
                slices_path=Path(args.slices), output_path=Path(args.output), default_adapter=args.adapter,
                jobs=Path(args.jobs) if args.jobs else None,
            ), sort_keys=True))
        except StageSessionError as exc:
            print(json.dumps({
                "planned": "refused", "reason": str(exc),
                "fallback": "single-session-required",
                "next_action": single_session_next_action(
                    args.route, args.node, args.node, args.parent or "<owner-slug>"),
            }, sort_keys=True))
            return 65
        return 0
    args.parent = args.parent or os.environ.get("AGENT_DISPATCH_SELF_SLUG")
    if not args.manifest or not args.parent:
        p.error(f"{args.action} requires --manifest and --parent")
    try:
        envelope = json.loads(Path(args.manifest).resolve().read_text(encoding="utf-8"))
        route_path = Path(envelope.get("route_file", "")).resolve()
        route_record = json.loads(route_path.read_text(encoding="utf-8"))
        route_record["_route_file"] = str(route_path)
        node = node_for(route_record, envelope.get("route_node", ""))
        manifest = load_manifest(args.manifest, route=route_record, node=node)
        verify = subprocess.run(
            [sys.executable, str(ROOT / "utilities/capability-route.py"), "verify",
             "--route", manifest["route_file"], "--cwd", manifest["worktree"],
             "--launch-phase", LAUNCH_PHASE_BY_ACTION[args.action]],
            cwd=ROOT, check=False,
        )
        if verify.returncode:
            return verify.returncode
        if args.action in {"check", "census"}:
            session_count = len(manifest["sessions"])
            if args.action == "census":
                jobs = resolve_global_registry(ROOT, args.jobs, 2, "check").path
                # Read-only measurement surface: the census consumer that
                # makes `runtime_joins` an observation of the owner-resume
                # ledger rather than a receipt constant (A-4).
                print(json.dumps({
                    "census": "ok", "chain_id": manifest["chain_id"],
                    "mode": manifest["mode"], "sessions": session_count,
                    **chain_census(Path(jobs), manifest["chain_id"], session_count),
                    "manifest_sha256": manifest["_manifest_sha256"],
                }, sort_keys=True))
                return 0
            print(json.dumps({
                "check": "ok", "chain_id": manifest["chain_id"],
                "mode": manifest["mode"], "sessions": session_count,
                **continuation_metrics(session_count),
                "manifest_sha256": manifest["_manifest_sha256"],
            }, sort_keys=True))
            return 0
        args.jobs = resolve_global_registry(ROOT, args.jobs, 2, args.action).path
        if manifest["mode"] != "serial":
            return _run_parallel_subdivision(route_record, node, args, jobs=Path(args.jobs))
        guarded_serial = manifest["mode"] == "serial" and len(manifest["sessions"]) >= 2
        owner_attempt_id = os.environ.get("AGENT_DISPATCH_ATTEMPT_ID", "")
        if guarded_serial and args.action in {"register", "start"}:
            probe = (
                probe_owner_supervision(Path(args.jobs), owner_attempt_id)
                if owner_attempt_id
                else SupervisionProbe("unsupervised", "owner-attempt-missing")
            )
            if probe.state != "held":
                print("check=failed")
                print(f"reason={_supervision_refusal_reason(probe.state)}")
                print(f"probe_reason={probe.reason or probe.state}")
                print("fallback=single-session-required")
                print("registered=0")
                print("started=0")
                print("child_spawned=0")
                return 65
        # SD-119 A-5 strengthening (plan WP5): the serial register loop must
        # be all-or-nothing like the parallel path already is
        # (`_run_parallel_subdivision` above) -- a mid-loop register failure
        # used to leave the already-registered rows live in the registry with
        # no rollback. Cancel every row registered so far and print the same
        # typed refusal envelope the parallel path prints, reusing
        # `BATCH_REGISTRATION_INCOMPLETE` rather than a new reason.
        registered_attempt_ids: list[str] = []
        for session in manifest["sessions"]:
            result = run_checked(
                dispatch_command(manifest, session, "register", args.parent, args.jobs)
            )
            if result.returncode:
                cancelled = 0
                if guarded_serial:
                    cancelled = len(close_refused_chain_rows(
                        Path(args.jobs), registered_attempt_ids,
                        note=SUBDIVISION_ADMISSION.BATCH_REGISTRATION_INCOMPLETE,
                        reconcile_reason=SUBDIVISION_ADMISSION.BATCH_REGISTRATION_INCOMPLETE,
                    ).cancelled)
                else:
                    for attempt_id in registered_attempt_ids:
                        cancelled += SUBDIVISION_ADMISSION._cancel_registered_row(Path(args.jobs), attempt_id)
                print(json.dumps({
                    "schema_version": 1, "state": "subdivision-batch-refused",
                    "chain_id": manifest["chain_id"],
                    "reason": SUBDIVISION_ADMISSION.BATCH_REGISTRATION_INCOMPLETE,
                    "admitted_rows": 0, "admitted_models": 0,
                    "cancelled_rows": cancelled,
                }, sort_keys=True))
                print(result.stdout, end="", file=sys.stderr)
                print(result.stderr, end="", file=sys.stderr)
                return 65
            registered_attempt_ids.append(session["attempt_id"])
        persist_chain_manifest(Path(args.jobs), manifest)
        if args.action == "register":
            print(f"chain_id={manifest['chain_id']}")
            print(f"registered_sessions={len(manifest['sessions'])}")
            return 0
        # action == "start": advance beyond index 1 is owned by the
        # non-model chain-advance checkpoint the supervisor drives internally
        # (dispatch_subsession_advance.py), never by this process waiting in
        # the foreground.
        first_session = manifest["sessions"][0]
        if guarded_serial:
            probe = probe_owner_supervision(Path(args.jobs), owner_attempt_id)
            if probe.state != "held":
                closed = close_refused_chain_rows(
                    Path(args.jobs), [s["attempt_id"] for s in manifest["sessions"]],
                    note=_supervision_refusal_reason(probe.state),
                    reconcile_reason=probe.reason or probe.state,
                )
                print("check=failed")
                print(f"reason={_supervision_refusal_reason(probe.state)}")
                print(f"probe_reason={probe.reason or probe.state}")
                print("registered=0")
                print("started=0")
                print("child_spawned=0")
                _print_refusal_tail(closed)
                return 65
        start_result = run_checked(
            dispatch_command(manifest, first_session, "start", args.parent, args.jobs)
        )
        if guarded_serial:
            parsed = parse_start_receipt(start_result.returncode, start_result.stdout, first_session["attempt_id"])
            if not parsed["ok"]:
                print(start_result.stdout, file=sys.stderr, end="")
                print(start_result.stderr, file=sys.stderr, end="")
                closed = close_refused_chain_rows(
                    Path(args.jobs), [s["attempt_id"] for s in manifest["sessions"]],
                    note="subsession-chain-advance-refused",
                    reconcile_reason="subsession-chain-initial-start-refused",
                )
                print("check=failed")
                print("reason=subsession-chain-initial-start-refused")
                print(f"start_verdict={parsed['verdict']}")
                print(f"wrapper_returncode={parsed['returncode']}")
                print(f"wrapper_reason={parsed['wrapper_reason']}")
                print(f"fallback={'single-session-after-delivery' if closed.unclosed else 'single-session-required'}")
                print("registered=0")
                print("started=0")
                print("child_spawned=0")
                print(f"cancelled_rows={len(closed.cancelled)}")
                print(f"already_closed_rows={len(closed.already_closed)}")
                print(f"unclosed_rows={len(closed.unclosed)}")
                print(f"unclosed_attempt_ids={','.join(closed.unclosed) or 'none'}")
                _print_refusal_tail(closed, include_fallback=False, include_counts=False)
                return 65
        if start_result.returncode:
            print(start_result.stdout, end="")
            print(start_result.stderr, end="", file=sys.stderr)
            return start_result.returncode
        print(f"chain_id={manifest['chain_id']}")
        print(f"chain_manifest_sha256={manifest['_manifest_sha256']}")
        print(f"registered_sessions={len(manifest['sessions'])}")
        print("registered=1")
        print("started=1")
        print(f"started_subsession_index={first_session['index']}")
        print("child_spawned=1")
        print("runtime_wait=registered-children")
        return 0
    except (OSError, ValueError, StageSessionError, DispatchContractError) as exc:
        print(f"stage-session-chain: {exc}", file=sys.stderr)
        return 65


if __name__ == "__main__":
    raise SystemExit(main())
