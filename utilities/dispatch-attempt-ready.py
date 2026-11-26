#!/usr/bin/env python3
"""Classify selected registry attempts without conflating terminal rows with exit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
from dispatch_contract import DispatchContractError  # noqa: E402
import dispatch_completion_join as JOIN  # noqa: E402
import dispatch_registry_cache as REGISTRY  # noqa: E402


def selected_rows(
    jobs: Path,
    *,
    parent: str = "",
    slug: str = "",
    attempt_id: str = "",
    read_context=None,
    append_only: bool = False,
) -> list[tuple[list[str], dict[str, str]]]:
    try:
        view = (read_context.registry_view(jobs, append_only=append_only)
                if read_context is not None
                else REGISTRY.registry_view(jobs, append_only=append_only))
    except FileNotFoundError:
        return []
    return view.select_rows(parent=parent, slug=slug, attempt_id=attempt_id)


def classify_legacy_rows(rows) -> dict[str, object]:
    """Compatibility for pre-registration rows with no governed identity."""
    children = [{"attempt_id": meta.get("attempt_id", "legacy"), "slug": fields[4],
                 "status": fields[1], "note": meta.get("note", "-"),
                 "readiness": "ready" if fields[1] == "done" else "process-unverifiable"
                     if fields[1] in {"open", "running"} else "contract-error"}
                for fields, meta in rows]
    state = ("terminal" if any(c["readiness"] == "contract-error" for c in children)
             else "pending" if any(c["readiness"] != "ready" for c in children) else "ready")
    return {"schema_version": 1, "state": state, "children": children}


def classify_selection(jobs: Path, rows, *, settle: bool = False,
                       observation_context=None, registry_append_only: bool = False) -> dict[str, object]:
    """Operational waits delegate registered decisions to the shared join."""
    registered = {metadata["attempt_id"] for _, metadata in rows
                  if metadata.get("attempt_schema_version") == "2"
                  and metadata.get("registered_worker", "").lower() in {"1", "true"}
                  and metadata.get("execution_surface") == "registered-headless"
                  and metadata.get("attempt_id")}
    legacy = classify_legacy_rows([(fields, metadata) for fields, metadata in rows
                       if metadata.get("attempt_id") not in registered])
    children = list(legacy["children"])
    joined = JOIN.join_selected_attempts(
        jobs=jobs, expected_attempts=registered, recover=settle,
        observation_context=observation_context,
        registry_append_only=registry_append_only,
    )
    for child in joined["children"]:
        current = dict(child)
        if child["readiness"] == "ready":
            row = JOIN.exact_attempt_row(jobs, child["attempt_id"])
            state = JOIN.current_delivery_state(
                jobs, row.attempt_id,
                # This wait owns the selected attempt and its descendants,
                # not all of that attempt's siblings under another parent.
                parent_attempt_id=row.attempt_id, advance=False,
            )
            current["status"] = state.status
            if state.cancellation_requested and not state.cancelled:
                current.update(readiness="pending", reason="termination-pending", required_action="")
                children.append(current)
                continue
            current["required_action"] = JOIN.delivery_required_action(state)
            current["readiness"] = (
                "ready" if JOIN.delivery_classification(state) == "success"
                else "pending" if not state.quiescent or state.owned_children or state.status in {"open", "running"}
                else "terminal-failure"
            )
        if current["readiness"] == "pending" and current["status"] in {"open", "running"} and child["readiness"] == "ready":
            current["reason"] = "terminal-commit-pending"
        children.append(current)
    state = ("terminal" if any(c["readiness"] in {"terminal-failure", "contract-error"} for c in children)
             else "pending" if any(c["readiness"] != "ready" for c in children) else "ready")
    return {"schema_version": 1, "state": state, "children": children,
            "recovery_diagnostics": joined.get("recovery_diagnostics", [])}


def archived_receipt(jobs: Path, attempt_id: str) -> dict[str, object] | None:
    """An explicitly named attempt whose rows the registry archive moved (settled for over
    14 days): answer from its last archived row, not from an empty selection, which reads as
    ready. Only a done row the shared policy reads as success is ready; any other archived
    end (a death, a cancel, a finished review or audit with findings) is a terminal result
    to inspect. None when the archive does not hold it."""
    try:
        from dispatch_attempt_policy import success_note, verdict_pass
        from dispatch_contract import parse_registry_metadata
        from dispatch_registry_archive import archived_rows
        lines = archived_rows(jobs, attempt_id)
    except Exception:  # noqa: BLE001 -- an unreadable archive leaves the old answer
        return None
    last = None
    for line in lines:
        fields = line.split("\t")
        if len(fields) == 6:
            metadata = parse_registry_metadata(fields[5])
            if metadata.get("attempt_id") == attempt_id:
                last = (fields, metadata)
    if last is None:
        return None
    fields, metadata = last
    note = metadata.get("note", "-")
    ready = fields[1] == "done" and (success_note(metadata) or verdict_pass(metadata))
    child = {"attempt_id": attempt_id, "slug": fields[4], "status": fields[1], "note": note,
             "readiness": "ready" if ready else "terminal-failure", "reason": "archived-row"}
    return {"schema_version": 1, "state": "ready" if ready else "terminal", "children": [child]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    from dispatch_contract import inherited_jobs_argument
    parser.add_argument("--jobs", **inherited_jobs_argument())
    parser.add_argument("--parent", default="")
    parser.add_argument("--slug", default="")
    parser.add_argument("--attempt-id", default="")
    parser.add_argument("--settle", action="store_true",
                        help="operational wait: commit exact outcomes through the shared runtime join")
    args = parser.parse_args()
    try:
        rows = selected_rows(
                Path(args.jobs),
                parent=args.parent,
                slug=args.slug,
                attempt_id=args.attempt_id,
            )
        receipt = (archived_receipt(Path(args.jobs), args.attempt_id)
                   if args.attempt_id and not rows else None)
        if receipt is None:
            receipt = classify_selection(Path(args.jobs), rows, settle=args.settle)
    except (OSError, JOIN.JoinContractError, DispatchContractError) as exc:
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
