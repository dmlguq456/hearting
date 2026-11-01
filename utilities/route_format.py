#!/usr/bin/env python3
"""The one reader of "does this release support the contract a route record declares".

The versions below are the single definition. `capability-route.py verify_route`,
the dispatch wrapper's admission, and the wrapper's choice of execution root all
ask this module, so a record format is judged by the same rule in each place.
Standard library only: another release's wrapper runs this file as a subprocess
(`python3 route_format.py <route_file>`) to ask what *that* release supports
without loading the 13k-line route compiler. Exit 0 = supported, 65 = unsupported
(reason on stdout), 66 = the record could not be read (no judgment).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROUTE_SCHEMA_VERSION = 2
DISPATCH_CONTRACT_VERSION = 3
DISPATCH_EVIDENCE_SCOPE_VERSION = 1
VALIDATION_BASIS_VERSION = 1
PERSONA_INDEPENDENCE_CONTRACT_VERSION = 1

BROKER_REASON = "legacy-broker-route-read-only"


def broker_format(route: dict) -> bool:
    """A pre-schema-2 record, or one that still carries the retired broker contract."""
    return route.get("schema_version") != ROUTE_SCHEMA_VERSION or "broker_contract_version" in route


def schema_reason(route: dict) -> str | None:
    if route.get("schema_version") != ROUTE_SCHEMA_VERSION:
        return (f"legacy route schema_version={route.get('schema_version')!r} "
                "rejected for mutating/resume use")
    return None


def contract_reason(route: dict) -> str | None:
    if route.get("dispatch_contract_version") != DISPATCH_CONTRACT_VERSION:
        return "legacy dispatch contract is read-only"
    return None


def scope_reason(route: dict) -> str | None:
    if route.get("dispatch_evidence_scope_version") not in (None, DISPATCH_EVIDENCE_SCOPE_VERSION):
        return "unsupported dispatch evidence scope version"
    return None


def basis_version_reason(version: int) -> str | None:
    if version > VALIDATION_BASIS_VERSION:
        return f"unsupported-validation-basis-version(basis_version={version})"
    return None


def persona_reason(route: dict) -> str | None:
    version = route.get("persona_independence_contract_version")
    if version is not None and (type(version) is not int
                                or version != PERSONA_INDEPENDENCE_CONTRACT_VERSION):
        return "unsupported-persona-independence-contract-version"
    return None


def unsupported_reason(route: dict, *, allow_stale_basis: bool = False) -> str | None:
    """Why this release cannot read the record, or None when it can."""
    if not isinstance(route, dict):
        return BROKER_REASON
    if broker_format(route):
        return BROKER_REASON
    for check in (contract_reason, scope_reason, persona_reason):
        reason = check(route)
        if reason:
            return reason
    basis = route.get("validation_basis")
    version = basis.get("basis_version") if isinstance(basis, dict) else None
    if not allow_stale_basis and isinstance(version, int) and not isinstance(version, bool):
        return basis_version_reason(version)
    return None


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        return 66
    try:
        route = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 66
    reason = unsupported_reason(route)
    if reason:
        print(reason)
        return 65
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
