"""The existing typed GPU lab selection, shared by probe and launch."""
from __future__ import annotations

import os

MODES = {"workspace-write", "read-only", "danger-full-access"}
RECEIPT_CODE = "codex-gpu-execution-sandbox"
LAB_EXECUTION_NODES = {"smoke", "full-run", "run-verify"}


def gpu_resource_nodes(route):
    if route.get("capability") != "autopilot-lab":
        return []
    signals = {row.get("signal") for row in
               (route.get("selection") or {}).get("promotion_signals", [])
               if isinstance(row, dict)}
    return [node["id"] for node in route.get("nodes", [])
            if isinstance(node, dict) and node.get("id")
            and node.get("kind") != "frame-worker"
            and not str(node.get("unit") or "").startswith("plan/frame")
            and ((node.get("parallel_anchor") or node["id"]) in LAB_EXECUTION_NODES
                 or node.get("resource_class") == "gpu"
                 or ("gpu" in signals and node.get("kind") == "resource-runner"))]


def select(route, *, node=None, owner=True, requested=None, environ=None):
    """Use typed route scope and existing overrides; never inspect task prose."""
    env = os.environ if environ is None else environ
    targets = gpu_resource_nodes(route)
    sealed = route.get("codex_execution_sandbox") or {}
    # A same-cycle suffix keeps its already selected owner runtime even when
    # its GPU resource is in the completed prefix. Normal suffix children do
    # not inherit the resource's full-access selection.
    inherited_owner = (owner and route.get("capability") == "autopilot-lab"
                       and route.get("continuation_contract_version") is not None
                       and sealed.get("gpu_scope") is True
                       and bool(sealed.get("gpu_resource_nodes")))
    scoped = (bool(targets) and (owner or node in targets)) or inherited_owner
    if inherited_owner and not targets:
        targets = list(sealed["gpu_resource_nodes"])
    mode = sealed.get("sandbox", "danger-full-access") if scoped else "workspace-write"
    source = sealed.get("source", "gpu-lab-resource") if scoped else "default"
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


def advisory(route, *, owner_harness=None, selection=None, applied=False):
    choice = selection or select(route)
    if not choice["gpu_scope"] or route.get("owner_dispatch_depth") == 0:
        return []
    owner = owner_harness or (route.get("work_request") or {}).get("owner_harness") or "auto"
    if owner not in {"auto", "codex"}:
        return []
    mode = choice["sandbox"]
    message = (f"Codex GPU lab: {mode} ({choice['source']}); 대상 owner 및 GPU 실행·검증 노드 "
               + ", ".join(choice["gpu_resource_nodes"]) + ". ")
    if mode == "danger-full-access":
        message += ("filesystem/network OS enforcement 없음; 요청 root와 child≤parent는 논리 경계입니다. ")
    else:
        message += "현재 Codex sandbox에서 NVIDIA 장치가 보이지 않을 수 있습니다. "
    message += "외부 sandbox·관리된 runtime 제약은 유지되며 GPU 조회 성공을 보증하지 않습니다."
    return [{"code": RECEIPT_CODE, "phase": "applied" if applied else "prospective",
             "owner_harness": owner, **choice, "message": message}]
