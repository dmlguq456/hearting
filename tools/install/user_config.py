"""Registry of every user-owned configuration surface the harness reads.

Install, `harness verify`, and `harness config status` all walk this one list,
so a surface that is added here is seeded, checked, and explained in the same
places. Each entry names where the file lives, what happens when it is absent,
and who creates it. Nothing here writes: seeding stays with each module's
``ensure`` so install order and dry-run semantics are unchanged.
"""

from __future__ import annotations

import compute_hosts_config
import memory_sync_config
import paths
import report_bundle_config
import routing_config
import user_model_config  # noqa: F401  (documents the models.conf owner)


# status -> (ok, short human reading)
READINGS = {
    "valid": (True, "ok"),
    "ok": (True, "ok"),
    "template": (True, "template — edit before use"),
    "absent": (True, "absent — optional"),
    "missing": (False, "missing — run `harness install`"),
    "invalid": (False, "invalid"),
    "drift": (True, "valid — drift warnings, see detail"),
    "shipped-default": (True, "shipped default in use"),
}


def _models_conf(runtime):
    def validate():
        path = paths.runtime_home(runtime) / "agent-config" / "models.conf"
        if path.is_symlink() or (path.exists() and not path.is_file()):
            return {"status": "invalid", "ok": False, "path": str(path),
                    "detail": "models.conf must be a regular file"}
        if path.is_file():
            return {"status": "valid", "ok": True, "path": str(path), "detail": "user copy"}
        return {"status": "shipped-default", "ok": True, "path": str(path),
                "detail": "adapters/<runtime>/config/models.conf applies until seeded"}
    return validate


SURFACES = (
    {
        "id": "compute-hosts",
        "title": "compute-host inventory",
        "seeded_by": "harness install (commented template, once)",
        "when_absent": "compute-hosts and the Fleet panel report it unconfigured",
        "validate": compute_hosts_config.validate,
    },
    {
        "id": "dispatch-defaults",
        "title": "dispatch routing policy",
        "seeded_by": "harness install (from activated runtimes, once)",
        "when_absent": "shipped profiles/dispatch-defaults.yaml applies",
        "validate": routing_config.validate,
    },
    {
        "id": "report-bundle",
        "title": "report bundle root",
        "seeded_by": "harness install (XDG data home, once)",
        "when_absent": "report-bundle tools refuse to run",
        "validate": report_bundle_config.validate,
    },
    {
        "id": "memory-sync",
        "title": "memory exchange policy",
        "seeded_by": "harness memory join --remote-url <git>",
        "when_absent": "memory stays local to this host",
        "validate": memory_sync_config.validate,
    },
) + tuple(
    {
        "id": f"models-conf.{runtime}",
        "title": f"{runtime} model roles",
        "seeded_by": f"harness install / runtime activate {runtime} (hard link, once)",
        "when_absent": "shipped adapter default applies",
        "validate": _models_conf(runtime),
    }
    for runtime in ("claude", "codex", "opencode")
)


def status(ids=None) -> list[dict]:
    rows = []
    for surface in SURFACES:
        if ids and surface["id"] not in ids:
            continue
        try:
            result = surface["validate"]()
        except Exception as exc:  # a broken checker is itself a finding
            result = {"status": "invalid", "path": "?", "detail": str(exc)}
        state = result.get("status", "invalid")
        ok, reading = READINGS.get(state, (False, state))
        ok = bool(result.get("ok", ok))
        if state == "missing" and ok:
            reading = "missing — optional; `harness install` seeds a template"
        rows.append({
            "id": surface["id"],
            "title": surface["title"],
            "status": state,
            "ok": ok,
            "reading": reading,
            "path": result.get("path", ""),
            "detail": result.get("detail", ""),
            "seeded_by": surface["seeded_by"],
            "when_absent": surface["when_absent"],
        })
    return rows


def lines(rows, *, verbose=False) -> list[str]:
    out = []
    for row in rows:
        mark = "!" if row["status"] == "drift" else ("✓" if row["ok"] else "✗")
        out.append(f"{mark} {row['id']:<20} {row['reading']:<28} {row['path']}")
        if verbose or row["status"] in {"template", "missing", "absent", "invalid", "drift"}:
            if row["detail"]:
                out.append(f"    {row['detail']}")
            if row["status"] in {"missing", "absent", "template"}:
                out.append(f"    seeded by: {row['seeded_by']}")
                out.append(f"    when absent: {row['when_absent']}")
    return out
