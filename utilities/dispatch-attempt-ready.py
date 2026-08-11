#!/usr/bin/env python3
"""Classify selected registry attempts without conflating terminal rows with exit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
from dispatch_contract import observed_attempt_liveness, parse_registry_metadata  # noqa: E402
from codex_dispatch_terminal import terminal_envelope_observed  # noqa: E402


def selected_rows(
    jobs: Path,
    *,
    parent: str = "",
    slug: str = "",
    attempt_id: str = "",
) -> list[tuple[list[str], dict[str, str]]]:
    try:
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []
    latest: dict[tuple[str, ...], tuple[list[str], dict[str, str]]] = {}
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = parse_registry_metadata(fields[5])
        if parent and metadata.get("parent") != parent:
            continue
        if slug and fields[4] != slug:
            continue
        if attempt_id and metadata.get("attempt_id") != attempt_id:
            continue
        key = (
            ("attempt", metadata["attempt_id"])
            if metadata.get("attempt_id")
            else ("legacy", fields[2], fields[3], fields[4])
        )
        latest[key] = (fields, metadata)
    return list(latest.values())


def classify(rows: list[tuple[list[str], dict[str, str]]]) -> dict[str, object]:
    children: list[dict[str, str]] = []
    terminal_failure = False
    pending = False
    for fields, metadata in rows:
        status = fields[1]
        note = metadata.get("note", "")
        registered_process = (
            metadata.get("attempt_schema_version") == "2"
            and metadata.get("execution_surface") == "registered-headless"
            and metadata.get("registered_worker", "").lower() in {"1", "true"}
        )
        observed = (
            observed_attempt_liveness(
                status,
                metadata,
                terminal_envelope=terminal_envelope_observed(
                    metadata.get("log_file")
                ),
            )
            if registered_process
            else None
        )
        if registered_process and observed.state in {"alive", "unverifiable"}:
            readiness = "pending"
            pending = True
        elif status in {"open", "running"}:
            # Legacy and non-registered rows have no governed process identity.
            # They remain pending while open; guessing terminal from transcript
            # age would reintroduce the semantic/process conflation this helper
            # exists to remove.
            if registered_process and observed.state == "reconcile-needed":
                readiness = "terminal-unclosed"
                terminal_failure = True
            else:
                readiness = "process-unverifiable"
                pending = True
        elif status == "done" and (
            note == "completed-marker"
            or metadata.get("attempt_schema_version") != "2"
            or (
                note == "completed-supervisor"
                and metadata.get("failure_class") == "pass"
                and (
                    metadata.get("worker_type") == "owner"
                    or (
                        metadata.get("subsession_id")
                        and metadata.get("stage_authority") in {"0", "false"}
                    )
                )
            )
        ):
            readiness = "ready"
        elif status == "done":
            readiness = "terminal-failure"
            terminal_failure = True
        else:
            readiness = "contract-error"
            terminal_failure = True
        children.append(
            {
                "attempt_id": metadata.get("attempt_id", "legacy"),
                "slug": fields[4],
                "status": status,
                "note": note or "-",
                "process_state": observed.process_state if observed else "not-applicable",
                "process_reason": observed.process_reason if observed else "unregistered-or-legacy",
                "observed_liveness": observed.state if observed else "not-applicable",
                "observed_reason": observed.reason if observed else "unregistered-or-legacy",
                "readiness": readiness,
            }
        )
    state = "terminal" if terminal_failure else "pending" if pending else "ready"
    return {"schema_version": 1, "state": state, "children": children}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", required=True)
    parser.add_argument("--parent", default="")
    parser.add_argument("--slug", default="")
    parser.add_argument("--attempt-id", default="")
    args = parser.parse_args()
    try:
        receipt = classify(
            selected_rows(
                Path(args.jobs),
                parent=args.parent,
                slug=args.slug,
                attempt_id=args.attempt_id,
            )
        )
    except OSError as exc:
        receipt = {
            "schema_version": 1,
            "state": "contract-error",
            "reason": str(exc),
            "children": [],
        }
    print(json.dumps(receipt, separators=(",", ":"), sort_keys=True))
    return {"ready": 0, "pending": 2, "terminal": 3}.get(str(receipt["state"]), 69)


if __name__ == "__main__":
    raise SystemExit(main())
