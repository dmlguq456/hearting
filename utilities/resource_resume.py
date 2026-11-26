"""The explicit small lab graph; execution still belongs to the shared runner/supervisor."""
from __future__ import annotations

import copy
import json
import hashlib
from pathlib import Path

GRAPH = ["resume-run", "run-verify"]


def row_digest(row):
    return hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def supervisor_alive(supervision):
    import resource_run_registry as RR
    return isinstance(supervision, dict) and RR.classify_identity(supervision)[0] == "working"


def selected(capability, mode, graph):
    return capability == "autopilot-lab" and mode == "setup" and graph == GRAPH


def recipe_selected(recipe):
    return (isinstance(recipe, dict)
            and selected(recipe.get("capability"), "setup", (recipe.get("compose") or {}).get("graph"))
            and recipe.get("modes") == ["setup"]
            and [n.get("id") for n in recipe.get("standard_plus", {}).get("nodes", [])] == GRAPH)


def route_selected(route):
    # One judgment with the compiler's `form == "solo"`: one owner (A) runs this verification alone.
    from route_form import one_shot_owner
    return (one_shot_owner(route)
            and recipe_selected(route.get("composed_recipe"))
            and [n.get("id") for n in route.get("nodes", [])] == ["resume-run", "one-shot"])


def nodes(recipe, owner_profile):
    resource, verification = copy.deepcopy(recipe["standard_plus"]["nodes"])
    if (resource.get("resource_policy") != "verified-resume"
            or resource.get("kind") != "resource-runner"):
        raise ValueError("resume-resource-contract-invalid")
    return [resource, {
        "id": "one-shot", "kind": "capability-owner", "dispatch_depth": 1,
        "role": "orchestrator", "depends_on": ["resume-run"],
        "unit": "_kernel/owner", "worker_type": "owner", "model_profile": owner_profile,
        "inputs": verification["inputs"], "outputs": verification["outputs"],
        "write_scope": verification["write_scope"], "resource_class": "normal",
        "execution_surface": "registered-headless", "registered_worker": True,
        "completion_gate": "quick-complete", "terminal": True,
        "terminal_gate": "quick-complete", "verification_only": True,
    }]


def observation(route, jobs):
    """Read the normal supervisor ledger; no status word alone admits verification."""
    import workflow_state as WS
    import resource_run_registry as RR
    ledger = WS.WorkflowLedger(route["route_id"], route["route_hash"], jobs=jobs)
    armed = ledger.root / "armed" / "resume-run.json"
    if not armed.exists():
        return {"state": "resource-ready", "reason": "resource-not-started"}
    try:
        watch = json.loads(armed.read_text())
        if (watch.get("route_id") != route["route_id"] or watch.get("route_hash") != route["route_hash"]
                or watch.get("node") != "resume-run" or watch.get("predecessor_kind") != "resource"
                or watch.get("jobs") != str(Path(jobs).resolve())):
            raise ValueError("resource-watch-binding-mismatch")
        registry = Path(watch["resource_registry"])
        row = json.loads(registry.read_text())["runs"][watch["predecessor_id"]]
        if (row.get("route") != watch["route_file"] or row.get("node") != "resume-run"
                or row.get("jobs") != watch["jobs"]):
            raise ValueError("resource-run-binding-mismatch")
        stage = ledger.state().get("nodes", {}).get("resume-run", {})
        identity = f"{row['run_id']}:{row.get('pid')}:{row.get('starttime')}:{row.get('exit_code')}"
        evidence = stage.get("evidence") or {}
        sentinel = Path(row["sentinel"])
        try:
            sentinel_zero = sentinel.read_text().strip() == "0"
        except OSError:
            sentinel_zero = False
        artifacts = evidence.get("artifacts") or {}
        proven = (stage.get("state") == "STAGE_SUCCEEDED" and row.get("status") == "succeeded"
                  and row.get("cancel_requested") is not True
                  and row.get("exit_code") == 0 and evidence.get("identity") == identity
                  and evidence.get("sentinel_present") is True
                  and sentinel_zero and evidence.get("resource_sha256") == row_digest(row)
                  and artifacts.get("checked") is True and not artifacts.get("missing")
                  and RR.classify_identity(row)[0] == "exited")
        return {"state": "resource-succeeded" if proven else "resource-running"
                if row.get("status") in ("launching", "running") else "needs-attention",
                "reason": "supervisor-evidenced-exit" if proven else "resource-not-proven-success",
                "resource_registry": str(registry), "run_id": row["run_id"],
                "resource": row, "supervision": row.get("supervision")}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"state": "needs-attention", "reason": type(exc).__name__ + ":resource-evidence-unavailable"}


def verification_prompt(route, jobs):
    current = observation(route, jobs)
    if current["state"] != "resource-succeeded":
        raise ValueError("resume-resource-success-required")
    return ("\n\nThis one-shot is ONLY the independent post-run verification. The approved payload "
            "has already executed; do not resume, extend, repeat or launch it again. Read the exact "
            f"resource registry {current['resource_registry']} run {current['run_id']}, its sentinel, "
            "logs and declared artifacts. Verify the requested run result independently, write "
            "reviews/run-verdict.json and report the actual verdict. A successful exit alone is "
            "not a verification PASS. Do not perform scaffold, smoke or new training work.")
