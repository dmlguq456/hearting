#!/usr/bin/env python3
"""Claude UserPromptSubmit (``start``) and Stop (``end``) bridge for the automatic change
record (utilities/turn_change_record.py): starts its detached step and stays silent."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "utilities"))


def main(argv) -> int:
    try:
        payload = json.load(sys.stdin)
        import turn_change_record
        turn_change_record.launch(argv[0] if argv else "", "claude", payload)
    except Exception:  # noqa: BLE001 -- a turn never depends on its record
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
