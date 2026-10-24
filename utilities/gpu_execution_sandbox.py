"""Typed GPU sandbox selection, shared by route compilation and launch."""
from __future__ import annotations

import os

from execution_access import compute_execution_scope

MODES = {"workspace-write", "read-only", "danger-full-access"}
RECEIPT_CODE = "codex-gpu-execution-sandbox"


def fresh_gpu_resource_nodes(route):
    """Judge new routes from their catalog parts and existing typed GPU markers."""
    signals = {row.get("signal") for row in
               (route.get("selection") or {}).get("promotion_signals", [])
               if isinstance(row, dict)}
    result = []
    for node in route.get("nodes", []):
        if not isinstance(node, dict) or not node.get("id"):
            continue
        if (node.get("kind") == "frame-worker"
                or str(node.get("unit") or "").startswith("plan/frame")):
            continue
        catalog_compute = compute_execution_scope(route, node=node["id"], owner=False)
        typed_gpu = node.get("resource_class") == "gpu"
        signaled_runner = "gpu" in signals and node.get("kind") == "resource-runner"
        if catalog_compute or typed_gpu or signaled_runner:
            result.append(node["id"])
    return result


def gpu_resource_nodes(route):
    """Return runtime GPU targets, honoring a seal before any fresh judgment."""
    sealed = route.get("codex_execution_sandbox")
    current_ids = {node.get("id") for node in route.get("nodes", []) if isinstance(node, dict)}
    if isinstance(sealed, dict):
        return [node for node in sealed.get("gpu_resource_nodes", [])
                if node in current_ids]
    # A pre-seal route with checked Codex evidence is historical. Do not grant
    # newly cataloged nodes GPU access or a lease retroactively.
    rows = (route.get("dispatch_evidence") or {}).get("tuples", [])
    if any(isinstance(row, dict) and row.get("parent_harness") == "codex" for row in rows):
        return []
    return fresh_gpu_resource_nodes(route)


def _choice(route, *, node, owner, requested, environ, historical):
    env = os.environ if environ is None else environ
    sealed = route.get("codex_execution_sandbox") or {}
    tuples = (route.get("dispatch_evidence") or {}).get("tuples", [])
    codex_rows = [row for row in tuples if isinstance(row, dict)
                  and row.get("parent_harness") == "codex"]
    current_ids = {item.get("id") for item in route.get("nodes", []) if isinstance(item, dict)}
    if historical and isinstance(sealed, dict) and sealed:
        all_targets = list(sealed.get("gpu_resource_nodes") or [])
        current_targets = [target for target in all_targets if target in current_ids]
        targets = all_targets if owner else current_targets
        scoped = bool(sealed.get("gpu_scope")) and (owner or node in current_targets)
        if owner and not current_targets and route.get("continuation_contract_version") is not None:
            scoped = bool(sealed.get("gpu_scope"))
        mode = sealed.get("sandbox", "workspace-write") if scoped else "workspace-write"
        source = sealed.get("source", "sealed-selection") if scoped else "default"
    elif historical and codex_rows:
        # Before the explicit sandbox seal existed, checked tuples did not
        # select an owner sandbox. Keep those routes on the ordinary path;
        # tuple/runtime agreement remains the dispatch contract's concern.
        targets = []
        scoped = False
        mode = "workspace-write"
        source = "default"
    else:
        targets = fresh_gpu_resource_nodes(route)
        scoped = (bool(targets) and (owner or node in targets))
        mode = "danger-full-access" if scoped else "workspace-write"
        source = "gpu-compute-resource" if scoped else "default"

    if env.get("CODEX_DISPATCH_SANDBOX"):
        mode, source = env["CODEX_DISPATCH_SANDBOX"], "caller-env"
    if requested is not None:
        mode, source = requested, "caller-cli"
    if env.get("CODEX_DISPATCH_SANDBOX_FORCE"):
        mode, source = env["CODEX_DISPATCH_SANDBOX_FORCE"], "forced-env"
    if mode not in MODES:
        raise ValueError("invalid-forced-dispatch-sandbox" if source == "forced-env"
                         else "invalid-dispatch-sandbox")
    return {"sandbox": mode, "source": source, "gpu_scope": scoped,
            "gpu_resource_nodes": targets,
            "file_enforcement": "none" if mode == "danger-full-access" else "os-sandbox",
            "network_enforcement": "none" if mode == "danger-full-access" else "os-sandbox"}


def select(route, *, node=None, owner=True, requested=None, environ=None):
    """Consume the sealed or historical route selection before considering new parts."""
    return _choice(route, node=node, owner=owner, requested=requested,
                   environ=environ, historical=True)


def select_fresh(route, *, node=None, owner=True, requested=None, environ=None):
    """Select during compile/readiness using only the typed catalog view."""
    return _choice(route, node=node, owner=owner, requested=requested,
                   environ=environ, historical=False)


def advisory(route, *, owner_harness=None, selection=None, applied=False):
    choice = selection or select(route)
    if not choice["gpu_scope"] or route.get("owner_dispatch_depth") == 0:
        return []
    owner = owner_harness or (route.get("work_request") or {}).get("owner_harness") or "auto"
    if owner not in {"auto", "codex"}:
        return []
    mode = choice["sandbox"]
    message = (f"Codex compute GPU: {mode} ({choice['source']}); 대상 owner와 compute resource node: "
               + ", ".join(choice["gpu_resource_nodes"]) + ". ")
    if mode == "danger-full-access":
        message += "filesystem/network OS enforcement가 없습니다. 승인된 root와 child≤parent는 논리적 경계입니다. "
    else:
        message += "선택된 Codex sandbox에서 NVIDIA 장치가 보이지 않을 수 있습니다. "
    message += "외부 또는 관리형 runtime 제약은 계속 적용되며, 선택이 GPU 사용 가능성을 보장하지는 않습니다."
    return [{"code": RECEIPT_CODE, "phase": "applied" if applied else "prospective",
             "owner_harness": owner, **choice, "message": message}]
