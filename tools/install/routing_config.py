"""Create the user-owned routing policy once from activated runtimes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import paths

_OWNER_ROUTE_BINDING_SCAN_LIMIT = 50


def _dispatch_contract_module():
    """Best-effort dynamic import of utilities/dispatch_contract.py, the same
    resolver chain the peer ledger and stable-registry writer use. Import
    failure is a caller concern (fail-soft `unknown`), never raised here."""
    utilities_dir = Path(__file__).resolve().parents[2] / "utilities"
    if str(utilities_dir) not in sys.path:
        sys.path.insert(0, str(utilities_dir))
    import dispatch_contract
    return dispatch_contract


def _newest_owner_route_binding(limit=_OWNER_ROUTE_BINDING_SCAN_LIMIT):
    """A5 evidence source: `<dispatch state root>/owner-route-bindings/*.json`,
    newest by the record's own `published_at` field (an explicit ordering key,
    not mtime). Returns None for every failure mode -- absent bindings dir,
    empty dir, or a scan that cannot resolve the state root -- so the caller
    always reports `unknown` rather than raising or guessing `drift`."""
    try:
        DC = _dispatch_contract_module()
        agent_home = Path(DC.resolve_agent_home())
        state_root = DC.resolve_dispatch_state_root(agent_home, environ=os.environ)
        bindings_dir = state_root / "owner-route-bindings"
        if not bindings_dir.is_dir():
            return None
        entries = []
        for path in sorted(bindings_dir.glob("*.json"))[:limit]:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload.get("published_at"), (int, float)):
                entries.append(payload)
        if not entries:
            return None
        entries.sort(key=lambda row: row["published_at"])
        return entries[-1]
    except Exception:
        return None


def confirmation_mode_drift_warning(declared_mode):
    """A5: declared-vs-sealed `confirmation_mode` drift, non-fatal.

    Compares the declared `confirmation.mode` against the most recently
    published owner route's sealed `confirmation_mode`. Every failure mode --
    no bindings published yet (a direct/quick-only user, or a fresh install),
    a pruned or unreadable `route_file`, or a legacy route sealed before this
    cycle with no `confirmation_mode` key -- reports nothing (`unknown`,
    never `drift`). Only an actually-differing, readable, newest binding
    returns a warning string.
    """
    binding = _newest_owner_route_binding()
    if binding is None:
        return None
    route_file = binding.get("route_file")
    if not route_file:
        return None
    try:
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    sealed_mode = route.get("confirmation_mode")
    if sealed_mode is None or sealed_mode == declared_mode:
        return None
    return (
        f"confirmation.mode={declared_mode!r} differs from the most recently "
        f"sealed route's confirmation_mode={sealed_mode!r} ({route_file})"
    )


RUNTIMES = ("claude", "codex", "opencode")


def config_path() -> Path:
    return paths.hearting_config_dir() / "dispatch-defaults.yaml"


def available_runtimes(targets) -> list[str]:
    ordered = [name for name in RUNTIMES if name in targets]
    discovered = [name for name in ordered if shutil.which(name)]
    return discovered or ordered


def render(enabled) -> str:
    enabled = [name for name in RUNTIMES if name in enabled]
    if not enabled:
        raise ValueError("routing config requires at least one enabled runtime")
    peers = [name for name in enabled if name != "opencode"]
    opencode = ["opencode"] if "opencode" in enabled else []
    if not peers:
        raise ValueError(
            "routing config requires at least one quality-peer runtime (claude or codex)"
        )

    def inline(values):
        return "[" + ", ".join(values) + "]"

    lines = [
        "# User-owned Hearting routing policy. Install/update never overwrites this file.",
        "schema_version: 3",
        "harnesses:",
        f"  enabled: {inline(enabled)}",
        "profiles:",
    ]
    for profile in ("deep", "balanced-deep", "light", "mini"):
        light = profile in {"light", "mini"}
        lines += [
            f"  {profile}:",
            f"    primary: {inline(peers + opencode if light else peers)}",
            f"    relief: {inline([])}",
            f"    last_resort: {inline([] if light else opencode)}",
            f"    promote_relief_below: 0",
        ]
    lines += [
        "allocation:",
        "  strategy: balanced",
        "  window: 30",
        "  usage_gate_used_percent: 85",
        "  depth_affinity:",
        *[f"    {depth}: {harness}" for depth, harness in (("owner", "claude"), ("worker", "codex")) if harness in enabled],
        "  depth_affinity_weight: 0.65",
        "  usage_headroom_exponent: 2",
        # Omitted cells inherit the shipped profiles/dispatch-defaults.yaml
        # capability baseline; a cell written here always wins over it.
        "capabilities:",
        "",
    ]
    return "\n".join(lines)


def ensure(targets, *, dry_run=False) -> dict:
    path = config_path()
    enabled = available_runtimes(targets)
    if path.exists():
        return {"status": "preserved", "path": str(path), "enabled": enabled}
    if not any(name in enabled for name in ("claude", "codex")):
        return {
            "status": "skipped-no-quality-peer",
            "path": str(path),
            "enabled": enabled,
        }
    if dry_run:
        return {"status": "would-create", "path": str(path), "enabled": enabled}
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        return {"status": "preserved", "path": str(path), "enabled": enabled}
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(render(enabled))
    return {"status": "created", "path": str(path), "enabled": enabled}


def validate() -> dict:
    path = config_path()
    if not path.is_file():
        return {"status": "missing", "ok": False, "path": str(path), "detail": "not initialized"}
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parents[2] / "utilities" / "dispatch-defaults.py"),
         "validate", "--config", str(path)],
        text=True,
        capture_output=True,
        check=False,
    )
    detail = (result.stdout or result.stderr).strip()
    if result.returncode != 0:
        return {"status": "invalid", "ok": False, "path": str(path), "detail": detail}
    # `warning=` lines are the validator's drift findings: a legacy strategy
    # that never adopted the shipped default, or keys the strategy ignores.
    # Still ok (the file is valid) but surfaced as a distinct status so
    # install, `harness verify`, and `harness config status` all show it.
    warnings = [
        line[len("warning="):].strip()
        for line in result.stdout.splitlines()
        if line.startswith("warning=")
    ]
    # A5: declared-vs-sealed confirmation_mode drift, layered onto the same
    # `warning=`-style non-fatal extension `allocation_warnings` already
    # established -- the surface stays "dispatch-defaults", no new row.
    mode_result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parents[2] / "utilities" / "dispatch-defaults.py"),
         "confirmation-mode", "--config", str(path)],
        text=True,
        capture_output=True,
        check=False,
    )
    if mode_result.returncode == 0:
        declared_mode = mode_result.stdout.strip()
        drift = confirmation_mode_drift_warning(declared_mode)
        if drift:
            warnings.append(drift)
    if warnings:
        return {
            "status": "drift",
            "ok": True,
            "path": str(path),
            "detail": "; ".join(warnings),
            "warnings": warnings,
        }
    return {"status": "valid", "ok": True, "path": str(path), "detail": detail, "warnings": []}
