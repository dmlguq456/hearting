#!/usr/bin/env python3
"""SD-119 R3: chain-scoped handoff contract for a serial sub-session chain.

Index i flushes one handoff file immediately before its own terminal
envelope; index i+1's chain-advance start is hard-gated on that handoff
classifying `ok` (§13.35.1-(5)). A missing or stale handoff never silently
proceeds -- start 0, marker 0 -- because a sub-session inherits no context
across attempt boundaries other than what this file records.
"""

from __future__ import annotations

from pathlib import Path

SCHEMA_VERSION = 1

_REQUIRED_FIELDS = (
    "predecessor_attempt_id",
    "predecessor_subsession_id",
    "manifest_sha256",
)


def handoff_path(artifact_root: Path, route_id: str, chain_id: str) -> Path:
    return (
        Path(artifact_root) / ".runtime" / "stage-sessions" / route_id
        / f"{chain_id}.handoff.md"
    )


def _render_frontmatter(fields: dict) -> str:
    lines = ["---"]
    for key in _REQUIRED_FIELDS:
        lines.append(f"{key}: {fields[key]}")
    lines.append(f"schema_version: {SCHEMA_VERSION}")
    lines.append("---")
    return "\n".join(lines)


def flush_handoff(
    path: Path,
    *,
    predecessor_attempt_id: str,
    predecessor_subsession_id: str,
    manifest_sha256: str,
    completed_items: list[str],
    next_command: str,
    invariants: list[str],
    forbidden_files: list[str],
) -> None:
    """Write the chain-scoped handoff. Called by index i just before its own
    terminal envelope closes -- the write must land before that attempt's
    process exits, or index i+1's `classify_handoff` sees `missing`."""

    fields = {
        "predecessor_attempt_id": predecessor_attempt_id,
        "predecessor_subsession_id": predecessor_subsession_id,
        "manifest_sha256": manifest_sha256,
    }
    body = [
        _render_frontmatter(fields),
        "",
        "## Completed",
        *(f"- {item}" for item in completed_items),
        "",
        "## Next command (exact)",
        "",
        "```",
        next_command,
        "```",
        "",
        "## Invariants",
        *(f"- {item}" for item in invariants),
        "",
        "## Forbidden files",
        *(f"- {item}" for item in forbidden_files),
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(body), encoding="utf-8")


def _parse_frontmatter(text: str) -> dict:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()
    return fields


def classify_handoff(
    path: Path,
    *,
    predecessor_attempt_id_expected: str,
    manifest_sha256_expected: str,
    predecessor_terminal_at_ns: int,
) -> str:
    """§13.35.1-(5): `ok` | `subsession-handoff-missing` | `subsession-handoff-stale`.

    Stale conditions (any one is sufficient):
    (1) the handoff's own `predecessor_attempt_id` does not match the
        registry's index-i terminal attempt id;
    (2) the handoff's `manifest_sha256` does not match the sealed chain
        manifest hash;
    (3) the handoff file's mtime is earlier than index i's terminal moment
        (written before completion, so it cannot describe that completion).
    """

    if not path.is_file():
        return "subsession-handoff-missing"
    try:
        text = path.read_text(encoding="utf-8")
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return "subsession-handoff-missing"
    fields = _parse_frontmatter(text)
    if fields.get("predecessor_attempt_id") != predecessor_attempt_id_expected:
        return "subsession-handoff-stale"
    if fields.get("manifest_sha256") != manifest_sha256_expected:
        return "subsession-handoff-stale"
    if mtime_ns < predecessor_terminal_at_ns:
        return "subsession-handoff-stale"
    return "ok"
