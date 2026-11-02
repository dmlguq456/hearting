"""Same-work identity drift diagnostics shared by dispatch consumers.

Dispatch proceeds on current inputs when a sealed identity differs from the
live one inside the approved work. The one signal is a single stderr line per
distinct diagnostic.
"""

import sys

_REPORTED = set()


def note_same_work_drift(reason, detail=""):
    message = " ".join(f"hearting: gate-off {reason} {detail}".splitlines())
    # Long-lived readers (Fleet ticks every 2 s) meet the same stale row again and
    # again; one line per distinct diagnostic is the whole signal.
    if message in _REPORTED:
        return
    _REPORTED.add(message)
    print(message, file=sys.stderr)
