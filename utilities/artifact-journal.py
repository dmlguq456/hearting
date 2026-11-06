#!/usr/bin/env python3
"""Read daily records or explicitly consolidate/restore verified small cycles."""
import argparse
import json
from pathlib import Path

import artifact_journal as journal
import artifact_journal_migration as migration


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("list", "dry-run", "apply", "restore"))
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--cycle")
    args = parser.parse_args()
    if args.action in {"apply", "restore"} and not args.cycle:
        parser.error("apply/restore need the selected --cycle")
    try:
        result = (journal.rows(args.artifact_root) if args.action == "list" else
                  migration.dry_run(args.artifact_root) if args.action == "dry-run" else
                  getattr(migration, args.action.replace("-", "_"))(args.artifact_root, args.cycle))
    except (OSError, ValueError) as exc:
        print(json.dumps({"state": "partial", "reason": str(exc)}))
        return 65
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
