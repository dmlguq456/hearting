#!/usr/bin/env python3
"""Register and supervise one serial execution chain beneath a route stage."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
from stage_session_contract import StageSessionError, load_manifest  # noqa: E402
from dispatch_contract import (  # noqa: E402
    DispatchContractError,
    GOVERNOR_RESERVATION_ENV,
    resolve_global_registry,
    resolve_model_governor_root,
)
import subdivision_batch_admission as SUBDIVISION_ADMISSION  # noqa: E402

_BATCH_SPEC = importlib.util.spec_from_file_location(
    "dispatch_batch_for_stage_session_chain", ROOT / "utilities" / "dispatch-batch.py"
)
if _BATCH_SPEC is None or _BATCH_SPEC.loader is None:
    raise ImportError("dispatch-batch.py could not be loaded")
DISPATCH_BATCH = importlib.util.module_from_spec(_BATCH_SPEC)
_BATCH_SPEC.loader.exec_module(DISPATCH_BATCH)  # type: ignore[union-attr]


def continuation_metrics(session_count: int) -> dict[str, int]:
    """Return the projected (pre-execution) join comparison for `check`.

    Actual `runtime_joins` is derived post-execution from the owner-resume
    census (see dispatch_subsession_resume_record.py) and is not a value this
    dry-run projection can assert.
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


def dispatch_command(
    manifest: dict, session: dict, action: str, parent: str, jobs: Path
) -> list[str]:
    command = [
        sys.executable, str(ROOT / "utilities" / "dispatch-node.py"),
        "--route", manifest["route_file"],
        "--node", manifest["route_node"],
        "--adapter", session["adapter"],
        "--action", action,
        "--slug", session["slug"],
        "--parent", parent,
        "--jobs", str(jobs),
        "--prompt-text", (
            f"Execute sub-session {session['subsession_id']} from phase brief "
            f"{session['phase_brief']}. Run only: {session['narrow_verify']}"
        ),
        "--subsession-id", session["subsession_id"],
        "--subsession-index", str(session["index"]),
        "--subsession-count", str(session["count"]),
        "--subsession-mode", manifest["mode"],
        "--session-chain-id", manifest["chain_id"],
        "--phase-brief", session["phase_brief"],
        "--stage-authority", "0",
        "--narrow-verify", session["narrow_verify"],
        "--expected-round-trips", str(session["expected_round_trips"]),
        "--attempt-id", session["attempt_id"],
    ]
    for file in session["fixed_files"]:
        command += ["--fixed-file", file]
    return command


def run_checked(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)


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
    path.write_text(json.dumps(manifest, sort_keys=True, default=str) + "\n", encoding="utf-8")


LAUNCH_PHASE_BY_ACTION = {
    "check": "dry-run",
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
        admission = SUBDIVISION_ADMISSION.admit_batch(
            route=route_record, node=node, manifest_path=args.manifest,
            governor=governor, governor_root=governor_root,
            reserve=DISPATCH_BATCH.reserve_batch,
        )
    except SUBDIVISION_ADMISSION.SubdivisionAdmissionError as exc:
        print(json.dumps({
            "schema_version": 1, "state": "subdivision-batch-refused",
            "chain_id": None, "reason": exc.reason,
            "admitted_rows": 0, "admitted_models": 0,
        }, sort_keys=True))
        return 65
    if args.action == "register":
        print(f"chain_id={admission.manifest['chain_id']}")
        print(f"registered_sessions={len(admission.sessions)}")
        return 0
    results = SUBDIVISION_ADMISSION.start_admitted_batch(
        admission, parent=args.parent, jobs=jobs,
        governor_reservation_env=GOVERNOR_RESERVATION_ENV,
    )
    print(f"chain_id={admission.manifest['chain_id']}")
    print(f"chain_manifest_sha256={admission.manifest_digest}")
    print(f"registered_sessions={len(admission.sessions)}")
    print(f"registered={sum(1 for row in results if row.get('registered'))}")
    print(f"started={sum(1 for row in results if row.get('started'))}")
    print(f"child_spawned={sum(1 for row in results if row.get('started'))}")
    print("runtime_wait=registered-children")
    return 0 if all(row.get("started") for row in results) else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("check", "register", "start"))
    p.add_argument("--manifest", required=True)
    p.add_argument("--parent", required=True)
    p.add_argument("--jobs")
    args = p.parse_args()
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
        if args.action == "check":
            session_count = len(manifest["sessions"])
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
        for session in manifest["sessions"]:
            result = run_checked(
                dispatch_command(manifest, session, "register", args.parent, args.jobs)
            )
            if result.returncode:
                print(result.stdout, end="")
                print(result.stderr, end="", file=sys.stderr)
                return result.returncode
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
        start_result = run_checked(
            dispatch_command(manifest, first_session, "start", args.parent, args.jobs)
        )
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
