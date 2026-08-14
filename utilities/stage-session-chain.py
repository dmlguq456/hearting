#!/usr/bin/env python3
"""Register and supervise one serial execution chain beneath a route stage."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
from stage_session_contract import StageSessionError, load_manifest  # noqa: E402
from dispatch_contract import (  # noqa: E402
    DispatchContractError,
    resolve_global_registry,
)


def continuation_metrics(session_count: int) -> dict[str, int]:
    """Return the M3+ comparison without depending on a live target checkout."""
    return {
        "baseline_runtime_joins": session_count,
        "runtime_joins": 1,
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


def readiness(jobs: Path, attempt_id: str) -> tuple[int, dict]:
    result = subprocess.run(
        [sys.executable, str(ROOT / "utilities" / "dispatch-attempt-ready.py"),
         "--jobs", str(jobs), "--attempt-id", attempt_id],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    try:
        return result.returncode, json.loads(result.stdout)
    except ValueError:
        return 69, {"state": "contract-error", "detail": result.stderr or result.stdout}


def supervise(manifest: dict, parent: str, jobs: Path, max_seconds: int) -> int:
    if manifest["mode"] != "serial":
        raise StageSessionError("parallel-subsession-use-dispatch-batch")
    deadline = time.monotonic() + max_seconds
    receipt = {"schema_version": 1, "chain_id": manifest["chain_id"], "sessions": []}
    for session in manifest["sessions"]:
        result = run_checked(
            dispatch_command(manifest, session, "start", parent, jobs)
        )
        if result.returncode != 0:
            receipt["sessions"].append({
                "attempt_id": session["attempt_id"], "state": "launch-failed",
                "exit_code": result.returncode,
                "detail": (result.stderr or result.stdout)[-2000:],
            })
            break
        while True:
            code, state = readiness(jobs, session["attempt_id"])
            if code == 0:
                receipt["sessions"].append({"attempt_id": session["attempt_id"], "state": "ready"})
                break
            if code != 2 or time.monotonic() >= deadline:
                receipt["sessions"].append({
                    "attempt_id": session["attempt_id"],
                    "state": "timeout" if code == 2 else "terminal-failure",
                    "readiness": state,
                })
                break
            time.sleep(2)
        if receipt["sessions"][-1]["state"] != "ready":
            break
    receipt["complete"] = len(receipt["sessions"]) == len(manifest["sessions"]) and all(
        item["state"] == "ready" for item in receipt["sessions"]
    )
    receipt_path = Path(manifest["_manifest_path"]).with_suffix(".receipt.json")
    receipt_path.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return 0 if receipt["complete"] else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("check", "register", "start", "run"))
    p.add_argument("--manifest", required=True)
    p.add_argument("--parent", required=True)
    p.add_argument("--jobs")
    p.add_argument("--max-seconds", type=int, default=3600)
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
             "--route", manifest["route_file"], "--cwd", manifest["worktree"]],
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
        args.jobs = resolve_global_registry(
            ROOT,
            args.jobs,
            2,
            "start" if args.action == "run" else args.action,
        ).path
        if manifest["mode"] != "serial":
            raise StageSessionError("parallel-subsession-use-dispatch-batch")
        if args.action in {"register", "start"}:
            for session in manifest["sessions"]:
                result = run_checked(
                    dispatch_command(
                        manifest, session, "register", args.parent, args.jobs
                    )
                )
                if result.returncode:
                    print(result.stdout, end="")
                    print(result.stderr, end="", file=sys.stderr)
                    return result.returncode
            print(f"chain_id={manifest['chain_id']}")
            print(f"registered_sessions={len(manifest['sessions'])}")
            print("runtime_joins=1")
            if args.action == "register":
                return 0
            # Keep the depth-1 owner process alive and join the entire serial
            # chain in this one tool call. Detaching here would create an
            # unsupervised continuation between the chain and its stage gate.
            result = supervise(manifest, args.parent, Path(args.jobs), args.max_seconds)
            receipt_path = Path(manifest["_manifest_path"]).with_suffix(".receipt.json")
            print(f"chain_receipt={receipt_path}")
            return result
        return supervise(manifest, args.parent, Path(args.jobs), args.max_seconds)
    except (OSError, ValueError, StageSessionError, DispatchContractError) as exc:
        print(f"stage-session-chain: {exc}", file=sys.stderr)
        return 65


if __name__ == "__main__":
    raise SystemExit(main())
