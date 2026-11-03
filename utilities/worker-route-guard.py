#!/usr/bin/env python3
"""Validate an immutable route before a worker starts; never re-route it."""
from __future__ import annotations


import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("capability_route", ROOT / "utilities" / "capability-route.py")
ROUTE = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(ROUTE)
sys.path.insert(0, str(ROOT / "utilities"))
from hearting_gates import note_same_work_drift
from artifact_context import scope_covers, scope_values


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
    if branch == "DETACHED":
        raise _fail("unsafe-git-state",
                    f"detached HEAD in {cwd}; route worktrees, including spec-only work, require a branch. "
                    "Run git switch -c <new-branch> in that worktree to preserve HEAD and existing changes, "
                    "then retry dispatch")
    if not head: raise _fail("unsafe-git-state", "HEAD cannot be resolved")
    return {"repository": "git", "operation": operation, "branch": branch, "head": head}


def _scopes(value: str | None) -> list[str]:
    """The observed scope values: ``;``, ``,`` or white space separate them."""
    if not isinstance(value, str):
        return scope_values(value)
    return scope_values(value.replace(",", ";").replace(" ", ";"))


# topologies.json write_scope vocabulary realized: these are the only scopes that
# name the versioned worktree/target file being edited in place (git-committed),
# as opposed to artifact-root outputs (reviews/**, dev_logs/**, plan/**, ...).
# One definition of the rule, in the route contract module this guard already
# imports. The continuation builder asks the same question when it decides
# whether to keep a route's pin, and both must get the same answer.
_worktree_mutating_scope = ROUTE.worktree_mutating_scope


def validate_route_contract(route_path: str | Path, node_id: str, cwd: str | Path,
                            artifact_root: str | Path, capability: str | None = None,
                            intensity: str | None = None, write_scope: str | None = None,
                            route_id: str | None = None, route_hash: str | None = None,
                            registry_digest: str | None = None,
                            current_attempt: str | None = None,
                            model_role: str | None = None,
                            model_profile: str | None = None,
                            enforce_model_binding: bool = False,
                            launch_phase: str | None = None,
                            jobs: str | Path | None = None) -> tuple[dict, dict, dict]:
    path = Path(route_path)
    if not path.is_absolute() or not path.is_file():
        raise _fail("route-record-missing", f"route path must be an existing absolute file: {path}")
    try: raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc: raise _fail("route-record-invalid", str(exc)) from exc
    rid = raw.get("route_id", "unknown")
    # The route is verified where it was sealed. A worker in another worktree is the
    # same-work question the cwd check below answers (reported, not refused);
    # verifying at the worker's cwd instead rewrote the route cwd and then failed the
    # exact-worktree dispatch evidence (`dispatch-evidence-worktree-mismatch`).
    try: route = ROUTE.verify_route(raw, raw.get("cwd") or cwd)
    except (ValueError, KeyError) as exc: raise _fail("route-verification-failed", str(exc), rid) from exc
    if launch_phase is not None:
        compatible, mismatches = ROUTE.revalidate_launch_compatibility(route)
        if mismatches.get("tuple") == "absent-legacy":
            raise _fail("launch-compatibility-tuple-required", launch_phase, rid)
        if not compatible:
            raise _fail(
                "launch-runtime-root-mismatch",
                json.dumps({"phase": launch_phase, "mismatches": mismatches,
                            "recovery": ROUTE.runtime_root_hint(route)}, sort_keys=True),
                rid,
            )
    actual_cwd = Path(cwd)
    actual_root = Path(artifact_root)
    if not actual_cwd.is_absolute(): raise _fail("cwd-not-absolute", str(actual_cwd), rid)
    if not actual_root.is_absolute(): raise _fail("artifact-root-not-absolute", str(actual_root), rid)
    if actual_cwd.resolve() != Path(route["cwd"]).resolve():
        note_same_work_drift("route-cwd-mismatch", str(actual_cwd))
        route = dict(route, cwd=str(actual_cwd.resolve()))
    if actual_root.resolve() != Path(route["artifact_root"]).resolve():
        note_same_work_drift("route-artifact-root-mismatch", str(actual_root))
        route = dict(route, artifact_root=str(actual_root.resolve()))
    node = next((row for row in route["nodes"] if row["id"] == node_id), None)
    if node is None: raise _fail("route-node-mismatch", node_id, rid)
    checks = (("route-id-mismatch", route_id, route["route_id"]),
              ("route-hash-mismatch", route_hash, route["route_hash"]),
              ("registry-digest-mismatch", registry_digest, route["registry_digest"]),
              ("capability-reselection", capability, route["capability"]),
              ("intensity-reselection", intensity, route["effective_intensity"]))
    for reason, observed, expected in checks:
        if observed is not None and observed != expected:
            note_same_work_drift(reason, f"expected={expected} observed={observed}")
    if write_scope is not None:
        # A narrower or differently written value of the sealed scope is the same approved work; a
        # value outside what the node was sealed with is not. The node's own sealed values are what
        # the next launch passes, and this check grants no write (the write oracle does).
        outside = [value for value in _scopes(write_scope) if not scope_covers(value, node["write_scope"])]
        if outside:
            raise _fail("route-node-scope-mismatch",
                        f"observed-outside-sealed-node={outside} sealed={sorted(node['write_scope'])}", rid)
    if enforce_model_binding:
        for reason, observed, expected in (
            ("route-node-model-role-mismatch", model_role or None, node.get("model_role")),
            ("route-node-model-profile-mismatch", model_profile or None, node.get("model_profile")),
        ):
            if expected is not None and observed != expected:
                if reason == "route-node-model-profile-mismatch" and current_attempt and jobs:
                    from dispatch_contract import DispatchContractError
                    from dispatch_replacement import read_profile_transition
                    try:
                        transition = read_profile_transition(
                            jobs, route=route, node=node_id, attempt_id=current_attempt)
                    except (DispatchContractError, OSError):
                        transition = None
                    if transition and transition["from"] == expected and transition["to"] == observed:
                        continue
                raise _fail(reason, f"expected={expected} observed={observed}", rid)
    if route["tracking"] == "tracked":
        gate = route["tracked_gate_evidence"]
        if not gate["spec_read"]["satisfied"] or not gate["artifact_guard"]["satisfied"]:
            raise _fail("tracked-gate-evidence-missing", "spec_read/artifact_guard not satisfied", rid)
        if gate["workflow_mode"] != "tracked": raise _fail("tracked-mode-mismatch", gate["workflow_mode"], rid)
    git = _git_state(actual_cwd)
    if git["head"] == route["source_commit"]:
        git["source_lineage"] = {
            "kind": "exact", "sealed": route["source_commit"],
            "observed": git["head"], "distance": 0, "branch": git["branch"],
        }
    else:
        # SD-156: one lineage verdict replaces the SD-65/SD-67/SD-128/SD-133
        # position-and-retry-evidence branches. `exact`/`descendant` pass
        # regardless of node position or prior registry attempts -- the
        # continuation builder (`_continuation_source_commit`) already re-pins
        # to any descendant HEAD, so this guard's only remaining question is
        # whether the live tree is still on that sealed line of work at all.
        verdict = ROUTE.source_lineage_verdict(actual_cwd, route["source_commit"])
        observed = verdict.commits[0] if verdict.commits else git["head"]
        git["source_lineage"] = {
            "kind": verdict.kind, "sealed": route["source_commit"],
            "observed": observed, "distance": verdict.distance, "branch": verdict.branch,
        }
        if verdict.kind == "diverged":
            note_same_work_drift("route-source-commit-mismatch", f"expected={route['source_commit']} observed={observed}")
            route = dict(route, source_commit=observed)
        if verdict.kind == "unverifiable":
            # `unsafe-git-operation`/`unsafe-git-state` keep this guard's existing
            # vocabulary (`_git_state` already raises them earlier for the cases
            # it can see); any other unverifiable reason is the new, retryable
            # token -- never read as a pass.
            reason = (
                verdict.reason if verdict.reason in ("unsafe-git-operation", "unsafe-git-state")
                else "source-lineage-unverifiable"
            )
            raise _fail(reason, verdict.reason or "unverifiable", rid)
        # descendant: passes, any node, any position.
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
    parser.add_argument("--jobs")
    parser.add_argument("--launch-phase", choices=("dry-run", "register", "start"))
    args = parser.parse_args()
    try:
        route, node, git = validate_route_contract(args.route, args.node, args.cwd, args.artifact_root,
            args.capability, args.intensity, args.write_scope, args.route_id, args.route_hash, args.registry_digest,
            current_attempt=args.current_attempt, model_role=args.model_role,
            model_profile=args.model_profile, enforce_model_binding=True,
            launch_phase=args.launch_phase, jobs=args.jobs)
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
    source_lineage = git.pop("source_lineage", None)
    output = {"status":"ok","action":"consume-route-only","route_id":route["route_id"],
          "node_id":node["id"],"tracking":route["tracking"],"cwd":route["cwd"],
          "artifact_root":route["artifact_root"],"git":git}
    if source_lineage is not None:
        output["source_lineage"] = source_lineage
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__": raise SystemExit(main())
