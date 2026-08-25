#!/usr/bin/env python3
"""Validate an immutable route before a worker starts; never re-route it."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("capability_route", ROOT / "utilities" / "capability-route.py")
ROUTE = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(ROUTE)
FALLBACK_SPEC = importlib.util.spec_from_file_location("stage_dispatch_fallback", ROOT / "utilities" / "stage-dispatch-fallback.py")
FALLBACK = importlib.util.module_from_spec(FALLBACK_SPEC); FALLBACK_SPEC.loader.exec_module(FALLBACK)


class WorkerRouteError(ValueError):
    def __init__(self, reason: str, detail: str, route_id: str = "unknown"):
        super().__init__(detail); self.reason = reason; self.route_id = route_id


def _fail(reason: str, detail: str, route_id: str = "unknown") -> WorkerRouteError:
    return WorkerRouteError(reason, detail, route_id)


def _git_state(cwd: Path) -> dict[str, str]:
    probe = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--git-dir"], text=True, capture_output=True)
    if probe.returncode != 0:
        return {"repository": "non-git", "operation": "none", "branch": "non-git", "head": "unversioned"}
    git_dir = Path(probe.stdout.strip())
    if not git_dir.is_absolute(): git_dir = cwd / git_dir
    operation = "none"
    if (git_dir / "MERGE_HEAD").exists(): operation = "merge"
    elif (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists(): operation = "rebase"
    elif (git_dir / "CHERRY_PICK_HEAD").exists(): operation = "cherry-pick"
    branch = subprocess.run(["git", "-C", str(cwd), "symbolic-ref", "--quiet", "--short", "HEAD"], text=True, capture_output=True).stdout.strip() or "DETACHED"
    head_probe = subprocess.run(["git", "-C", str(cwd), "rev-parse", "HEAD"], text=True, capture_output=True)
    head = head_probe.stdout.strip() if head_probe.returncode == 0 else "unversioned"
    if operation != "none": raise _fail("unsafe-git-operation", operation)
    if branch == "DETACHED": raise _fail("unsafe-git-state", "detached HEAD")
    if not head: raise _fail("unsafe-git-state", "HEAD cannot be resolved")
    return {"repository": "git", "operation": operation, "branch": branch, "head": head}


def _scopes(value: str | None) -> list[str]:
    return sorted(part for part in (value or "").split(";") if part)


# topologies.json write_scope vocabulary realized: these are the only scopes that
# name the versioned worktree/target file being edited in place (git-committed),
# as opposed to artifact-root outputs (reviews/**, dev_logs/**, plan/**, ...).
def _worktree_mutating_scope(scope: str) -> bool:
    if scope in ("target-artifact", "source-scoped"): return True
    root = scope[:-3] if scope.endswith("/**") else scope
    return root == "source"


def _post_mutation_node(nodes: list[dict], node_id: str) -> bool:
    by_id = {row["id"]: row for row in nodes}
    mutating_ids = {row["id"] for row in nodes if any(_worktree_mutating_scope(s) for s in row.get("write_scope", []))}
    seen: set[str] = set()
    stack = list(by_id.get(node_id, {}).get("depends_on", []))
    while stack:
        dep = stack.pop()
        if dep in seen: continue
        seen.add(dep)
        if dep in mutating_ids: return True
        stack.extend(by_id.get(dep, {}).get("depends_on", []))
    return False


def _is_first_parent_descendant(cwd: Path, source_commit: str, head: str) -> bool:
    result = subprocess.run(["git", "-C", str(cwd), "rev-list", "--first-parent", head], text=True, capture_output=True)
    if result.returncode != 0: return False
    return source_commit in result.stdout.split()


def _direct_mutation_node(node: dict) -> bool:
    return any(_worktree_mutating_scope(s) for s in node.get("write_scope", []))


def _qualifying_retry_evidence(route_id: str, node_id: str, current_attempt: str | None) -> bool:
    """SD-67: a different prior global-registry attempt for the same route/node."""
    jobs_env = os.environ.get("AGENT_DISPATCH_JOBS")
    if not jobs_env: return False
    jobs_path = Path(jobs_env)
    if not jobs_path.is_absolute() or not jobs_path.is_file(): return False
    try:
        rows = FALLBACK.registry_rows(jobs_path, route_id, node_id)
    except (OSError, ValueError):
        return False
    current = next((row for row in rows if row.get("attempt_id") == current_attempt), None)
    if current and current.get("subsession_purpose") == "planned":
        return False
    return any(
        row.get("attempt_id")
        and row.get("attempt_id") != current_attempt
        and (
            not row.get("subsession_id")
            or row.get("subsession_purpose") == "gap-retry"
        )
        for row in rows
    )


def _qualifying_subsession_lineage(
    route_id: str, node_id: str, current_attempt: str | None
) -> bool:
    """A planned serial slice may consume a prior slice without becoming a retry."""
    jobs_env = os.environ.get("AGENT_DISPATCH_JOBS")
    if not jobs_env or not current_attempt:
        return False
    jobs_path = Path(jobs_env)
    if not jobs_path.is_absolute() or not jobs_path.is_file():
        return False
    try:
        rows = FALLBACK.registry_rows(jobs_path, route_id, node_id)
    except (OSError, ValueError):
        return False
    current = next((row for row in rows if row.get("attempt_id") == current_attempt), None)
    if not current or (
        current.get("stage_authority") not in {"0", "false"}
        or current.get("subsession_purpose") != "planned"
        or current.get("subsession_mode") != "serial"
        or not current.get("session_chain_id")
    ):
        return False
    try:
        current_index = int(current.get("subsession_index", "0"))
        current_count = int(current.get("subsession_count", "0"))
    except ValueError:
        return False
    if not 2 <= current_index <= current_count:
        return False
    return any(
        row.get("attempt_id") != current_attempt
        and row.get("session_chain_id") == current.get("session_chain_id")
        and row.get("subsession_purpose") == "planned"
        and row.get("stage_authority") in {"0", "false"}
        and row.get("_status") == "done"
        and row.get("failure_class") == "pass"
        and row.get("note") == "completed-supervisor"
        and row.get("subsession_count") == current.get("subsession_count")
        and str(row.get("subsession_index", "")).isdigit()
        and int(row["subsession_index"]) == current_index - 1
        for row in rows
    )


def validate_route_contract(route_path: str | Path, node_id: str, cwd: str | Path,
                            artifact_root: str | Path, capability: str | None = None,
                            intensity: str | None = None, write_scope: str | None = None,
                            route_id: str | None = None, route_hash: str | None = None,
                            registry_digest: str | None = None,
                            current_attempt: str | None = None,
                            model_role: str | None = None,
                            model_profile: str | None = None,
                            enforce_model_binding: bool = False,
                            launch_phase: str | None = None) -> tuple[dict, dict, dict]:
    path = Path(route_path)
    if not path.is_absolute() or not path.is_file():
        raise _fail("route-record-missing", f"route path must be an existing absolute file: {path}")
    try: raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc: raise _fail("route-record-invalid", str(exc)) from exc
    rid = raw.get("route_id", "unknown")
    try: route = ROUTE.verify_route(raw, cwd)
    except (ValueError, KeyError) as exc: raise _fail("route-verification-failed", str(exc), rid) from exc
    if launch_phase is not None:
        compatible, mismatches = ROUTE.revalidate_launch_compatibility(route)
        if mismatches.get("tuple") == "absent-legacy":
            raise _fail("launch-compatibility-tuple-required", launch_phase, rid)
        if not compatible:
            raise _fail(
                "launch-runtime-root-mismatch",
                json.dumps({"phase": launch_phase, "mismatches": mismatches}, sort_keys=True),
                rid,
            )
    actual_cwd = Path(cwd)
    actual_root = Path(artifact_root)
    if not actual_cwd.is_absolute(): raise _fail("cwd-not-absolute", str(actual_cwd), rid)
    if not actual_root.is_absolute(): raise _fail("artifact-root-not-absolute", str(actual_root), rid)
    if actual_cwd.resolve() != Path(route["cwd"]).resolve(): raise _fail("route-cwd-mismatch", str(actual_cwd), rid)
    if actual_root.resolve() != Path(route["artifact_root"]).resolve(): raise _fail("route-artifact-root-mismatch", str(actual_root), rid)
    node = next((row for row in route["nodes"] if row["id"] == node_id), None)
    if node is None: raise _fail("route-node-mismatch", node_id, rid)
    checks = (("route-id-mismatch", route_id, route["route_id"]),
              ("route-hash-mismatch", route_hash, route["route_hash"]),
              ("registry-digest-mismatch", registry_digest, route["registry_digest"]),
              ("capability-reselection", capability, route["capability"]),
              ("intensity-reselection", intensity, route["effective_intensity"]))
    for reason, observed, expected in checks:
        if observed is not None and observed != expected: raise _fail(reason, f"expected={expected} observed={observed}", rid)
    if write_scope is not None and _scopes(write_scope) != sorted(node["write_scope"]):
        raise _fail("route-node-scope-mismatch", f"expected={sorted(node['write_scope'])} observed={_scopes(write_scope)}", rid)
    if enforce_model_binding:
        for reason, observed, expected in (
            ("route-node-model-role-mismatch", model_role or None, node.get("model_role")),
            ("route-node-model-profile-mismatch", model_profile or None, node.get("model_profile")),
        ):
            if expected is not None and observed != expected:
                raise _fail(reason, f"expected={expected} observed={observed}", rid)
    if route["tracking"] == "tracked":
        gate = route["tracked_gate_evidence"]
        if not gate["spec_read"]["satisfied"] or not gate["artifact_guard"]["satisfied"]:
            raise _fail("tracked-gate-evidence-missing", "spec_read/artifact_guard not satisfied", rid)
        if gate["workflow_mode"] != "tracked": raise _fail("tracked-mode-mismatch", gate["workflow_mode"], rid)
    git = _git_state(actual_cwd)
    if git["head"] != route["source_commit"]:
        downstream_ok = (_post_mutation_node(route["nodes"], node_id)
                      and _is_first_parent_descendant(actual_cwd, route["source_commit"], git["head"]))
        mutation_retry_ok = (_direct_mutation_node(node)
                      and node_id in route.get("resume_retry_boundaries", ())
                      and _qualifying_retry_evidence(route["route_id"], node_id, current_attempt)
                      and _is_first_parent_descendant(actual_cwd, route["source_commit"], git["head"]))
        planned_subsession_ok = (_direct_mutation_node(node)
                      and node_id in route.get("resume_retry_boundaries", ())
                      and _qualifying_subsession_lineage(route["route_id"], node_id, current_attempt)
                      and _is_first_parent_descendant(actual_cwd, route["source_commit"], git["head"]))
        if not (downstream_ok or mutation_retry_ok or planned_subsession_ok):
            raise _fail("route-source-commit-mismatch", f"expected={route['source_commit']} observed={git['head']}", rid)
    return route, node, git


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("command", choices=("validate",))
    parser.add_argument("--route", required=True); parser.add_argument("--node", required=True)
    parser.add_argument("--cwd", required=True); parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--capability"); parser.add_argument("--intensity"); parser.add_argument("--write-scope")
    parser.add_argument("--route-id"); parser.add_argument("--route-hash"); parser.add_argument("--registry-digest")
    parser.add_argument("--model-role"); parser.add_argument("--model-profile")
    parser.add_argument("--unit", default=None,
                        help="catalog unit the caller intends to run; must match the sealed node")
    parser.add_argument("--current-attempt")
    parser.add_argument("--launch-phase", choices=("dry-run", "register", "start"))
    args = parser.parse_args()
    try:
        route, node, git = validate_route_contract(args.route, args.node, args.cwd, args.artifact_root,
            args.capability, args.intensity, args.write_scope, args.route_id, args.route_hash, args.registry_digest,
            current_attempt=args.current_attempt, model_role=args.model_role,
            model_profile=args.model_profile, enforce_model_binding=True,
            launch_phase=args.launch_phase)
        # Unit binding: a worker may not run a bare or substituted persona against a
        # sealed node (2026-07-22 verify finding). Empty/None observed == unbound claim.
        expected_unit = node.get("unit") or None
        observed_unit = args.unit or None
        if observed_unit != expected_unit:
            raise WorkerRouteError("route-node-unit-mismatch",
                f"expected={expected_unit} observed={observed_unit}", route.get("route_id"))
    except WorkerRouteError as exc:
        print(json.dumps({"status":"blocked","reason":exc.reason,"detail":str(exc),"route_id":exc.route_id,"route_file":args.route}, sort_keys=True), file=sys.stderr)
        return 65
    print(json.dumps({"status":"ok","action":"consume-route-only","route_id":route["route_id"],
          "node_id":node["id"],"tracking":route["tracking"],"cwd":route["cwd"],
          "artifact_root":route["artifact_root"],"git":git}, sort_keys=True))
    return 0


if __name__ == "__main__": raise SystemExit(main())
