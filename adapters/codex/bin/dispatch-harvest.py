#!/usr/bin/env python3
"""Codex dispatch registry harvest/status wrapper.

The harvest body is shared (`utilities/dispatch-harvest.py`); this wrapper keeps
only what is Codex's own: the `~/.codex/hearting` home pointer, the Codex runtime
registry a bare call falls back to after `--jobs` and the inherited
`AGENT_DISPATCH_JOBS`, and the names in its header.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "utilities"))
from dispatch_contract import resolve_agent_home as _resolve_agent_home  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "dispatch_harvest_shared", ROOT / "utilities" / "dispatch-harvest.py"
)
SHARED = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = SHARED
_spec.loader.exec_module(SHARED)


def resolve_agent_home() -> Path:
    return _resolve_agent_home(runtime_pointer=Path.home() / ".codex" / "hearting")


def default_runtime_jobs(environ: dict[str, str] | os._Environ[str]) -> Path:
    """Return the Codex runtime registry, never the packaged source root."""

    codex_home = Path(environ.get("CODEX_HOME") or Path.home() / ".codex")
    return codex_home.expanduser() / ".harness" / "dispatch" / "jobs.log"


CODEX = SHARED.Surface(
    adapter="codex",
    runtime_surface="codex-dispatch-harvest",
    cleanup_command="adapters/codex/bin/preflight.sh worktree-cleanup --check --worktree <path>",
    resolve_agent_home=resolve_agent_home,
    default_jobs=lambda _agent_home: default_runtime_jobs(os.environ),
)


def main(argv: list[str]) -> int:
    return SHARED.main(argv, CODEX)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
