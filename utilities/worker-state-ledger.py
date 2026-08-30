#!/usr/bin/env python3
"""Create and enforce the compact-safe worker state ledger."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import tempfile

PREFIX = "<!-- worker-state-ledger:v1 "
SUFFIX = " -->"
MAX_BYTES = 64 * 1024
MAX_EDITS = 3


class LedgerError(ValueError):
    pass


def _read(path: Path) -> tuple[dict, str]:
    try:
        if path.is_symlink() or path.stat().st_size > MAX_BYTES:
            raise LedgerError("ledger-unsafe")
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LedgerError("ledger-missing") from exc
    first = text.splitlines()[0] if text else ""
    if not first.startswith(PREFIX) or not first.endswith(SUFFIX):
        raise LedgerError("ledger-header-invalid")
    try:
        meta = json.loads(first[len(PREFIX):-len(SUFFIX)])
    except (ValueError, TypeError) as exc:
        raise LedgerError("ledger-metadata-invalid") from exc
    required = {
        "schema_version", "attempt_id", "generation", "edits_since_update",
        "verification_pending", "resume_required", "fixed_files",
    }
    if meta.get("schema_version") != 1 or not required <= set(meta):
        raise LedgerError("ledger-contract-incomplete")
    return meta, text


def _render(meta: dict, fields: dict[str, object]) -> str:
    header = PREFIX + json.dumps(meta, sort_keys=True, separators=(",", ":")) + SUFFIX
    bullets = lambda values: "\n".join(f"- {value}" for value in values) or "- none"
    return (
        f"{header}\n# Worker State Ledger\n\n"
        f"## Current slice\n{fields.get('current_slice') or 'unspecified'}\n\n"
        f"## Completed items\n{bullets(fields.get('completed_items') or [])}\n\n"
        f"## Exact next command\n`{fields.get('next_action') or 'none'}`\n\n"
        f"## Invariants\n{bullets(fields.get('invariants') or [])}\n\n"
        f"## Fixed files\n{bullets(meta.get('fixed_files') or [])}\n\n"
        f"## Forbidden files\n{bullets(fields.get('forbidden_files') or [])}\n"
    )


def _body_fields(text: str) -> dict[str, object]:
    sections: dict[str, list[str]] = {}
    current = ""
    for line in text.splitlines()[1:]:
        if line.startswith("## "):
            current = line[3:].strip().lower()
            sections[current] = []
        elif current:
            sections[current].append(line)

    def plain(name: str) -> str:
        return "\n".join(sections.get(name, [])).strip().strip("`")

    def items(name: str) -> list[str]:
        return [line[2:] for line in sections.get(name, []) if line.startswith("- ") and line != "- none"]

    return {
        "current_slice": plain("current slice"),
        "completed_items": items("completed items"),
        "next_action": plain("exact next command"),
        "invariants": items("invariants"),
        "forbidden_files": items("forbidden files"),
    }


def read_fields(path: Path, attempt_id: str) -> dict[str, object]:
    """Public accessor for a sub-session's own ledger body fields, used by
    SD-119 R3 chain-scoped handoff synthesis. Returns {} for a missing,
    unreadable, or attempt-mismatched ledger -- callers treat that as
    "nothing to carry forward", never as a hard failure."""

    try:
        meta, text = _read(Path(path))
    except LedgerError:
        return {}
    if meta.get("attempt_id") != attempt_id:
        return {}
    return _body_fields(text)


def _atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _locked(path: Path):
    lock_path = Path(f"{path}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def _validate(meta: dict, attempt_id: str) -> None:
    if not attempt_id or meta.get("attempt_id") != attempt_id:
        raise LedgerError("ledger-attempt-mismatch")
    if meta.get("resume_required"):
        raise LedgerError("ledger-postcompact-reread-required")
    if int(meta.get("edits_since_update", MAX_EDITS)) >= MAX_EDITS:
        raise LedgerError("ledger-update-overdue")
    if meta.get("verification_pending"):
        raise LedgerError("ledger-verification-update-required")


def command(args: argparse.Namespace) -> str:
    path = Path(args.path).resolve()
    with _locked(path):
        if args.command == "init":
            if path.exists():
                meta, _ = _read(path)
                if meta.get("attempt_id") != args.attempt_id:
                    raise LedgerError("ledger-attempt-mismatch")
                return f"ledger={path}\nstatus=existing"
            meta = {
                "schema_version": 1,
                "attempt_id": args.attempt_id,
                "generation": 1,
                "edits_since_update": 0,
                "verification_pending": False,
                "resume_required": False,
                "fixed_files": sorted(str(Path(value).resolve(strict=False)) for value in args.fixed_file),
            }
            fields = {
                "current_slice": args.current_slice,
                "completed_items": [],
                "next_action": args.next_action,
                "invariants": args.invariant,
                "forbidden_files": args.forbidden_file,
            }
            _atomic(path, _render(meta, fields))
            return f"ledger={path}\nstatus=created"

        meta, text = _read(path)
        if meta.get("attempt_id") != args.attempt_id:
            raise LedgerError("ledger-attempt-mismatch")
        fields = _body_fields(text)
        if args.command in {"check", "guard-edit"}:
            _validate(meta, args.attempt_id)
            if args.file:
                target = str(Path(args.file).resolve(strict=False))
                if target not in meta.get("fixed_files", []):
                    raise LedgerError(f"file-outside-fixed-list:{target}")
            if args.command == "guard-edit":
                meta["edits_since_update"] = int(meta["edits_since_update"]) + 1
                _atomic(path, _render(meta, fields))
            return f"ledger={path}\nstatus=ok\nedits_since_update={meta['edits_since_update']}"
        if args.command == "update":
            meta.update(
                generation=int(meta["generation"]) + 1,
                edits_since_update=0,
                verification_pending=False,
                resume_required=False,
            )
            for name in ("current_slice", "next_action"):
                value = getattr(args, name)
                if value is not None:
                    fields[name] = value
            for name in ("completed_items", "invariants", "forbidden_files"):
                value = getattr(args, name[:-1] if name.endswith("s") else name, None)
                if value:
                    fields[name] = value
            _atomic(path, _render(meta, fields))
            return f"ledger={path}\nstatus=updated\ngeneration={meta['generation']}"
        if args.command == "mark-verification":
            _validate(meta, args.attempt_id)
            meta["verification_pending"] = True
            _atomic(path, _render(meta, fields))
            return f"ledger={path}\nstatus=verification-pending"
        if args.command == "flush":
            meta.update(
                generation=int(meta["generation"]) + 1,
                edits_since_update=0,
                verification_pending=False,
            )
            _atomic(path, _render(meta, fields))
            return f"ledger={path}\nstatus=flushed"
        if args.command == "compact-before":
            meta.update(
                generation=int(meta["generation"]) + 1,
                edits_since_update=0,
                verification_pending=False,
                resume_required=True,
            )
            _atomic(path, _render(meta, fields))
            return f"ledger={path}\nstatus=compact-flushed"
        if args.command == "compact-after":
            meta["resume_required"] = False
            bounded = _render(meta, fields)[:MAX_BYTES]
            _atomic(path, bounded)
            return f"ledger={path}\nstatus=reanchored\n{bounded}"
    raise LedgerError("command-unsupported")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=(
        "init", "check", "guard-edit", "update", "mark-verification",
        "flush", "compact-before", "compact-after",
    ))
    p.add_argument("--path", required=True)
    p.add_argument("--attempt-id", required=True)
    p.add_argument("--file")
    p.add_argument("--current-slice")
    p.add_argument("--next-action")
    p.add_argument("--completed-item", action="append", default=[])
    p.add_argument("--invariant", action="append", default=[])
    p.add_argument("--forbidden-file", action="append", default=[])
    p.add_argument("--fixed-file", action="append", default=[])
    return p


def main() -> int:
    args = parser().parse_args()
    # Keep argparse destinations explicit for the update path.
    args.completed_items = args.completed_item
    args.invariants = args.invariant
    args.forbidden_files = args.forbidden_file
    try:
        print(command(args))
        return 0
    except LedgerError as exc:
        print(f"worker-state-ledger: {exc}", file=os.sys.stderr)
        return 65


if __name__ == "__main__":
    raise SystemExit(main())
