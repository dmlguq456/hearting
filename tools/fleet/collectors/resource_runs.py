"""Fail-soft collector for indexed resource-runner registries."""
import sys
from contextlib import nullcontext
from pathlib import Path

from ..model import ResourceJob, project_of

_READ_CACHE = None


def _shared():
    root = Path(__file__).resolve().parents[3]
    utilities = str(root / "utilities")
    if utilities not in sys.path:
        sys.path.insert(0, utilities)
    import resource_run_registry
    return resource_run_registry


def collect(index_path=None):
    global _READ_CACHE
    rows = []
    diagnostics = []
    try:
        shared = _shared()
        scope = getattr(shared, 'passive_read_scope', None)
        if callable(scope) and _READ_CACHE is None:
            _READ_CACHE = shared.ResourceReadCache()
        with scope(_READ_CACHE) if callable(scope) else nullcontext():
            raw, diagnostics = shared.scan(index_path=index_path)
        projects = {}
        for item in raw:
            try:
                cwd = item.get("cwd")
                if cwd not in projects:
                    projects[cwd] = project_of(cwd)
                item["project"] = projects[cwd]
                rows.append(ResourceJob(**item))
            except Exception as exc:
                diagnostics.append({
                    "kind": "resource-row-projection",
                    "path": item.get("registry_path"),
                    "run_id": str(item.get("run_id")),
                    "error": str(exc),
                })
    except Exception as exc:
        diagnostics.append({"kind": "resource-collector", "error": str(exc)})
    collect.last_diagnostics = diagnostics
    return rows


collect.last_diagnostics = []
