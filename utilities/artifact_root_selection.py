"""Choose between the two report directories of a known project."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from artifact_identity import IdentityError, RootIdentity, read_root_identity

ARTIFACT_ROOT_NAMES = (".agent_reports", ".claude_reports")
PRODUCER_REL = ".runtime/artifact-producer/v1"


def cutover_path(root: Path) -> Path:
    return Path(root) / PRODUCER_REL / "cutover.json"


def read_cutover_record(root: Path) -> dict[str, Any] | None:
    path = cutover_path(root)
    try:
        if path.is_symlink() or not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def inspect_root_activation(
    root: Path,
) -> tuple[dict[str, Any] | None, RootIdentity | None, str | None]:
    """Return the cutover record, verified active identity, and malformed reason.

    Root selection needs the same activation check as classify_root, without
    traversing an inactive root's legacy contents.
    """
    cutover = None
    if os.path.exists(cutover_path(root)):
        cutover = read_cutover_record(root)
        if cutover is None:
            return None, None, "cutover-record-unreadable"
        if cutover.get("state") not in ("active", "inactive"):
            return cutover, None, "cutover-schema-unknown"
    try:
        identity = read_root_identity(root)
    except (OSError, ValueError, IdentityError):
        return cutover, None, "root-identity-invalid"
    if cutover is None or cutover["state"] != "active":
        return cutover, None, None
    recorded = cutover.get("identity")
    if (identity is None or not isinstance(recorded, dict)
            or recorded.get("artifact_root_id") != identity.artifact_root_id):
        return cutover, None, "identity-conflict"
    return cutover, identity, None


def is_activated_root(root: Path) -> bool:
    return inspect_root_activation(root)[1] is not None


def select_artifact_root(project: Path) -> Path:
    """Given the project directory, return its default report directory."""
    agent, claude = (Path(project) / name for name in ARTIFACT_ROOT_NAMES)
    if not os.path.isdir(claude):
        return agent
    if not os.path.isdir(agent):
        return claude
    if is_activated_root(agent) or not is_activated_root(claude):
        return agent
    return claude


if __name__ == "__main__":
    selected = select_artifact_root(Path(sys.argv[1]))
    print(os.path.realpath(selected) if os.path.isdir(selected) else selected)
