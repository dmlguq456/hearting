#!/usr/bin/env python3
"""d1/d2 performance regression for the user-path hooks at machine scale.

The fixture (``hook_perf_fixture.py``) holds the machine's measured global
state: sessions ledgers, handover snapshots, steward markers, deliveries, peer
history with its lock files, and a 55 MB registry with ~1,600 unrelated
depth-1 owner rows. Only the target session's own share and the registry's
size stay fixed when ``scale`` doubles; the registry's unrelated owner rows
double with the rest.

Every measured hook runs as a real subprocess with a test-only ``sitecustomize``
shim. The shim records every ``open``/``os.open``/``io.open`` and every byte
read to a JSONL sidecar, counts every directory listing (``os.listdir``,
``os.scandir``; pathlib ``iterdir``/``glob`` and ``os.walk`` go through them)
and every ``os.stat``/``os.lstat`` call, and attributes each to a record kind
(``jobs.log``, ``sessions-dir``, ``peer-pending``, ``pending-delivery``,
``recipient-inbox``, ``transcript``, ``other``). It also injects
``may_report=True`` (the product has no test flag), records that the Stop header
path reached ``is_steward``, stops the detached obligation runner, and replaces
any detached child with ``/bin/true``, so no background process mutates the
fixture (or anything outside it) while a sample is measured. A fake ``herdr``
executable records the argv it receives.

Time assertions (prompt and Stop only): prompt <= 3 s and Stop <= 2 s at scale
1; scale 2 may open at most 1.1x the files of scale 1; a delivered prompt pass
reads at most 6.5x the fixed registry size (the pre-fix path read it 13 times).
The scale-time delta (1.5x / +0.5 s) is reported, not asserted: it follows
machine load, and the count rules below bound the same growth.

Count assertions (every case, including the Python hooks a prompt, a tool call
and a turn end start for Claude and Codex): per record kind, opens and listings
at scale 2 stay within +2 of scale 1; one run opens ``jobs.log`` at most 16
times and lists the sessions lineage at most 20 times; total stat calls at scale
2 stay within 1.2x of scale 1. A hook that re-reads a growing record whole, or
re-scans it per row, fails here even when a fast machine hides the time.

Counts must not depend on machine load, so the shim lifts every hook time
budget that could cut a scan short (``LIFTED_BUDGETS`` and the peer module's
scan deadlines; each lift is asserted), and both scales must have audited the
same processes: a child killed by a timeout is a failed measurement, never a
fix. The cases that grow today are listed in ``KNOWN_GROWTH`` with their cause;
that table is a ratchet: a listed case must still break exactly its listed
rules (a fix fails the test until its entry is removed) and may grow at most
2.2x (a per-row walk still fails), every other case passes every rule, and the
ceilings bind all cases. Every check is unit-tested against over-budget samples
so the gate cannot pass vacuously.
"""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "utilities"))

import dispatch_pending_delivery as pending  # noqa: E402
import dispatch_recipient_inbox as inbox  # noqa: E402
import hook_perf_fixture as fixture_module  # noqa: E402

PROMPT_HOOK = ROOT / "hooks" / "dispatch-session-sweep.py"
STOP_HOOK = ROOT / "hooks" / "herdr-session-projection.py"
HOOKS = ROOT / "hooks"
CODEX_HOOKS = ROOT / "adapters" / "codex" / "hooks"
KIND = "claude-parent-runtime"
TARGET = fixture_module.TARGET_SESSION

PROMPT_LIMIT_S = 3.0
STOP_LIMIT_S = 2.0
SCALE_TIME_RATIO = 1.5
SCALE_TIME_SLACK_S = 0.5
SCALE_OPEN_RATIO = 1.1
# A delivered prompt pass used to read the fixed registry 13 times (13 x
# jobs.log, 670 MB on the 51.5 MB fixture). The delivery path now needs 6
# (discovery 1, claim 1 per group, effect 1 per group, render 1); 6.5 keeps a
# little slack for harmless one-off reads.
PROMPT_READ_RATIO = 6.5
RUNS = 5
USER_PATH_RUNS = 3

# Record kinds the audit shim attributes each open, listing, stat and byte to.
RECORD_KINDS = ("jobs.log", "sessions-dir", "peer-pending", "pending-delivery",
                "recipient-inbox", "transcript", "other")
KIND_FIELDS = ("opens", "listings", "stats", "bytes")
# Count rules (time-free, so a loaded machine cannot hide or fake them):
# * per record kind, opens and listings at scale 2 may exceed scale 1 by at
#   most KIND_SLACK. Scale 2 doubles every unrelated record; a hook whose
#   work follows its own share adds nothing, so +2 only absorbs a one-off
#   (a lock file or index created on first use).
KIND_SLACK = 2
# * `jobs.log` opened at most JOBS_OPEN_CEILING times by one hook run.
#   Measured on 2026-11-04 (v4/hook-latency): the delivered prompt pass opens
#   it 13 times for its two notices: one whole read through the registry
#   cache, and 12 identity/readability probes that read no registry bytes
#   (worker_home_retention at each claim and effect boundary, workflow_state
#   at route close). The asyncRewake scan opens it once, the Codex first
#   prompt twice, every other hook never. 16 is that maximum plus ~20%; a
#   re-read per registry row or per record (thousands) fails at once.
JOBS_OPEN_CEILING = 16
# * the sessions lineage directory listed at most SESSIONS_LIST_CEILING times
#   by one hook run. A read-only pass observes it once and pins that
#   observation (`PeerReadContext.pinned_lineage`); every effect boundary
#   reads it fresh. Measured maximum: 16 for the delivered prompt pass (10
#   fresh effect-boundary observations, 6 graph loads), 4 for the asyncRewake
#   scan. 20 is that maximum plus 25%; the pre-#1184 asyncRewake scan listed
#   it once per registry row (1,600 times) and fails at once.
SESSIONS_LIST_CEILING = 20
# * total os.stat/os.lstat calls at scale 2 at most 1.2x scale 1.
SCALE_STAT_RATIO = 1.2

PROMPT_CASES = ("prompt-empty", "prompt-two", "prompt-stale", "stop", "prompt-runner")
# Every Python hook a user's prompt, tool call or turn end starts:
# (case, hook, event, argv). The target owns no owner row, has no pending peer
# message and no saved start receipt, so each hook's own work is fixed and
# small; whatever grows from scale 1 to scale 2 is a walk over unrelated state.
USER_PATH_CASES = (
    # Claude: PreToolUse/PostToolUse Bash.
    ("route-gate", HOOKS / "route-presence-gate.py", "PreToolUse", ()),
    ("rewake-bash", HOOKS / "dispatch-owner-rewake.py", "PostToolUse", ()),
    ("steward-rewake", HOOKS / "peer-steward-rewake.py", "PostToolUse", ()),
    ("start-receipt", HOOKS / "start-receipt-context.py", "PostToolUse", ()),
    ("git-index-lock", HOOKS / "git-index-lock-context.py", "PostToolUse",
     ("--harness", "claude")),
    ("peer-post-tool", HOOKS / "peer-message-record.py", "PostToolUse", ("post-tool",)),
    ("interaction-clear", HOOKS / "fleet-interaction-state.py", "PostToolUse", ("clear",)),
    # Claude: UserPromptSubmit.
    ("peer-prompt", HOOKS / "peer-message-record.py", "UserPromptSubmit", ("prompt",)),
    ("turn-start", HOOKS / "turn-change-record.py", "UserPromptSubmit", ("start",)),
    # Claude: Stop.
    ("rewake-stop", HOOKS / "dispatch-owner-rewake.py", "Stop", ()),
    ("turn-end", HOOKS / "turn-change-record.py", "Stop", ("end",)),
    ("tidy-idle", HOOKS / "session-tidy-idle.py", "Stop", ()),
    ("cycle-checkpoint", HOOKS / "open-cycle-checkpoint.py", "Stop", ()),
    # Codex bridges on the same events.
    ("codex-prompt", CODEX_HOOKS / "userprompt-lifecycle.py", "UserPromptSubmit", ()),
    ("codex-post-tool", CODEX_HOOKS / "posttooluse-interaction-clear.py", "PostToolUse", ()),
    ("codex-start-receipt", CODEX_HOOKS / "start-receipt-context.py", "PostToolUse", ()),
    ("codex-stop", CODEX_HOOKS / "stop-lifecycle.py", "Stop", ()),
    ("codex-turn-post-tool", CODEX_HOOKS / "turn-carrier.py", "PostToolUse", ("post-tool",)),
    ("codex-turn-stop", CODEX_HOOKS / "turn-carrier.py", "Stop", ("stop",)),
)
GROWTH_RULES = ("growth", "stats")
# A listed case may keep its recorded linear growth, never more: scale 2 doubles
# the unrelated state, so per kind and for stat calls it stays within 2.2x of
# scale 1 (or +KIND_SLACK). A per-row walk over a record (rows x records) grows
# ~4x and fails, even in a listed case.
LISTED_GROWTH_RATIO = 2.2
# Hook time budgets the audit shim lifts (multiplied by 10**6 when the hook's
# main() starts), so a scan is never cut short by machine speed: the peer
# context budget (Claude prompt/post-tool, Codex post-tool), its watch-sweep
# share and exit guard, and the Stop header's idle-receive retry window. The
# peer module's own scans (`_pending_rows`, `undelivered_watch_rows`) also run
# without a deadline, which covers the Codex prompt's literal 2.5 s.
LIFTED_BUDGETS = ("_HOOK_BUDGET_S", "_SWEEP_SHARE_S", "_EXIT_AFTER_FLUSH_S", "_PEER_BUDGET_S",
                  "RETRY_BUDGET_NS")

# Measured against main (7f22a46ac, after #1186) on 2026-11-04.
_LINEAGE_WALKS = (
    "utilities/dispatch_seat_handover.py:379 - a notice-delivering prompt lists the lineage "
    "16 times: 10 fresh effect-boundary signatures (:379, through _pass_lineage_graph at "
    ":1635) and 3 compact graph loads that list it twice each (:253, :264); every listing "
    "lstats every ledger, so stat calls go 12.9k -> 23.5k - removed by the follow-up that "
    "lets the sweep's effect boundaries share one lineage observation")
_REWAKE_LISTINGS = (
    "utilities/dispatch_seat_handover.py:104 - the rewake scan reads the published compact "
    "rows and resolves its jobs path once (no ledger opens and no per-row path walk since "
    "HL-13), but its one pinned lineage observation and the compact graph check still list "
    "the lineage four times and lstat every ledger, so stat calls go 2.9k -> 5.5k - removed by "
    "the lineage-observation follow-up that detects writer changes without an lstat per ledger")
# Cases that grow with unrelated state today, with the rule(s) each breaks and
# the evidence. This is a ratchet, not an allowance: an entry must still break
# exactly its listed rules (a fix that makes one pass fails the test until the
# entry is narrowed or deleted) and grow at most LISTED_GROWTH_RATIO; every
# other case must pass every rule; the absolute ceilings hold for every case.
KNOWN_GROWTH = {
    "prompt-two": (("stats",), _LINEAGE_WALKS),
    "prompt-stale": (("stats",), _LINEAGE_WALKS),
    "prompt-runner": (("stats",), _LINEAGE_WALKS),
    "rewake-bash": (("stats",), _REWAKE_LISTINGS),
    "rewake-stop": (("stats",), _REWAKE_LISTINGS),
}
# The peer-module scans each case reaches; a renamed scan function would run
# with its 2.5 s deadline again, so the lift itself is asserted.
REQUIRED_MODULE_LIFTS = {
    "peer-post-tool": ("peer-message.py:_pending_rows",),
    "codex-post-tool": ("peer-message.py:_pending_rows",),
    "codex-prompt": ("peer-message.py:_pending_rows",),
    "peer-prompt": ("peer-message.py:_pending_rows", "peer-steward.py:undelivered_watch_rows"),
    # The Stop header's idle callback reads only tidy reservations now: peer rows are
    # delivered by the shell host, the native queue or the next callback, never here.
    "stop": (),
}


# ---------------------------------------------------------------------------
# Budget checks (unit-tested so an over-budget sample must fail)
# ---------------------------------------------------------------------------

def check_budget(name: str, seconds: float, limit_s: float) -> None:
    if seconds > limit_s:
        raise AssertionError(
            f"{name} budget exceeded: {seconds:.3f}s > {limit_s:.3f}s")


def check_read_budget(name: str, bytes_read: int, registry_bytes: int,
                      ratio: float = PROMPT_READ_RATIO) -> None:
    limit = int(registry_bytes * ratio)
    if bytes_read > limit:
        raise AssertionError(
            f"{name} reread the registry too often: {bytes_read} bytes > {limit} "
            f"({ratio}x {registry_bytes})")


def check_scale_time(name: str, base_s: float, scaled_s: float) -> None:
    if (scaled_s > base_s * SCALE_TIME_RATIO
            or scaled_s > base_s + SCALE_TIME_SLACK_S):
        raise AssertionError(
            f"{name} grew with unrelated state: {base_s:.3f}s -> {scaled_s:.3f}s "
            f"(limit {base_s * SCALE_TIME_RATIO:.3f}s and {base_s + SCALE_TIME_SLACK_S:.3f}s)")


def check_scale_opens(base: int, scaled: int) -> None:
    if scaled > base * SCALE_OPEN_RATIO:
        raise AssertionError(
            f"opened files grew with unrelated state: {base} -> {scaled}")


def check_kind_growth(name: str, base: dict, scaled: dict, slack: int = KIND_SLACK) -> None:
    """Per record kind, opens and listings do not grow with unrelated state."""
    grown = []
    for kind in sorted(set(base) | set(scaled)):
        for field in ("opens", "listings"):
            before = int(base.get(kind, {}).get(field, 0))
            after = int(scaled.get(kind, {}).get(field, 0))
            if after > before + slack:
                grown.append(f"{kind} {field} {before} -> {after}")
    if grown:
        raise AssertionError(
            f"{name} grew with unrelated state (more than +{slack}): " + "; ".join(grown))


def check_kind_ceilings(name: str, kinds: dict) -> None:
    """One hook run re-reads the registry and re-lists the lineage a bounded number of times."""
    over = []
    opened = int(kinds.get("jobs.log", {}).get("opens", 0))
    listed = int(kinds.get("sessions-dir", {}).get("listings", 0))
    if opened > JOBS_OPEN_CEILING:
        over.append(f"jobs.log opened {opened} times > {JOBS_OPEN_CEILING}")
    if listed > SESSIONS_LIST_CEILING:
        over.append(f"sessions lineage listed {listed} times > {SESSIONS_LIST_CEILING}")
    if over:
        raise AssertionError(f"{name}: " + "; ".join(over))


def check_scale_stats(name: str, base: int, scaled: int) -> None:
    if scaled > base * SCALE_STAT_RATIO:
        raise AssertionError(
            f"{name} stat calls grew with unrelated state: {base} -> {scaled} "
            f"(limit {base * SCALE_STAT_RATIO:.0f})")


def check_counts(name: str, run1: dict, run2: dict) -> None:
    """Every count rule for one case measured at scale 1 and scale 2."""
    check_kind_ceilings(f"{name} (scale 1)", run1["kinds"])
    check_kind_ceilings(f"{name} (scale 2)", run2["kinds"])
    check_kind_growth(name, run1["kinds"], run2["kinds"])
    check_scale_stats(name, run1["stats"], run2["stats"])


def check_growth_ratio(name: str, run1: dict, run2: dict,
                       ratio: float = LISTED_GROWTH_RATIO) -> None:
    """Per kind (opens, listings) and for stat calls, scale 2 stays within ``ratio`` x scale 1."""
    over = []
    kinds1, kinds2 = run1["kinds"], run2["kinds"]
    for kind in sorted(set(kinds1) | set(kinds2)):
        for field in ("opens", "listings"):
            before = int(kinds1.get(kind, {}).get(field, 0))
            after = int(kinds2.get(kind, {}).get(field, 0))
            if after > max(before * ratio, before + KIND_SLACK):
                over.append(f"{kind} {field} {before} -> {after}")
    if run2["stats"] > run1["stats"] * ratio:
        over.append(f"stat calls {run1['stats']} -> {run2['stats']}")
    if over:
        raise AssertionError(
            f"{name} grew faster than the unrelated state (more than {ratio}x), beyond its "
            "recorded KNOWN_GROWTH: " + "; ".join(over))


def check_lifts(name: str, hook: Path, run: dict, required: tuple = ()) -> None:
    """Every budget the hook script names ran lifted; else its counts follow machine speed."""
    source = Path(hook).read_text(encoding="utf-8")
    expected = [f"__main__:{budget}" for budget in LIFTED_BUDGETS if budget in source]
    missing = [item for item in (*expected, *required) if item not in run.get("lifted", ())]
    if missing:
        raise AssertionError(
            f"{name}: the shim did not lift {', '.join(missing)} (renamed, or no longer read "
            "in main()?); its counts would depend on machine speed")


def check_ratchet(name: str, run1: dict, run2: dict, known: dict | None = None) -> None:
    """``check_counts`` with the KNOWN_GROWTH ratchet.

    Both scales must have audited the same processes (a killed or skipped
    child is a failed measurement, never a fix). The ceilings hold for every
    case. A case outside the table passes every rule; a case in it breaks
    exactly the rules it lists and grows at most LISTED_GROWTH_RATIO, so a fix
    that makes one pass, a listed hook that starts breaking another rule, or
    one that starts walking per row fails here.
    """
    known = KNOWN_GROWTH if known is None else known
    if run1.get("processes") != run2.get("processes"):
        raise AssertionError(
            f"{name}: measurement failed, not fixed: {run1.get('processes')} audited processes "
            f"at scale 1 but {run2.get('processes')} at scale 2 (a child was killed, timed out "
            "or skipped, so its counts are missing)")
    check_kind_ceilings(f"{name} (scale 1)", run1["kinds"])
    check_kind_ceilings(f"{name} (scale 2)", run2["kinds"])
    expected, evidence = known.get(name, ((), ""))
    if expected:
        check_growth_ratio(name, run1, run2)
    checks = {
        "growth": lambda: check_kind_growth(name, run1["kinds"], run2["kinds"]),
        "stats": lambda: check_scale_stats(name, run1["stats"], run2["stats"]),
    }
    broken = {}
    for rule in GROWTH_RULES:
        try:
            checks[rule]()
        except AssertionError as error:
            broken[rule] = str(error)
    unlisted = [message for rule, message in broken.items() if rule not in expected]
    if unlisted:
        suffix = f" (KNOWN_GROWTH lists {name} only for {', '.join(expected)})" if expected else ""
        raise AssertionError("; ".join(unlisted) + suffix)
    fixed = [rule for rule in expected if rule not in broken]
    if fixed:
        action = ("delete its KNOWN_GROWTH entry" if len(fixed) == len(expected)
                  else "remove " + ", ".join(repr(rule) for rule in fixed)
                  + " from its KNOWN_GROWTH entry")
        raise AssertionError(
            f"{name} now passes the {', '.join(fixed)} rule: {action}. "
            f"Recorded cause: {evidence}")


# ---------------------------------------------------------------------------
# Test-only audit shim
# ---------------------------------------------------------------------------

_SHIM = r'''
"""Test-only hook observer. Never shipped; injected through PYTHONPATH."""
import atexit
import builtins
import io
import json
import os
import sys

_LOG = os.environ.get("HOOK_PERF_AUDIT_LOG")
# The audit covers the measured hook and the children it waits for, all in the hook's
# session. A detached daemon it starts (its own session) is not the hook's work and can
# outlive the fixture; its exit-time write would recreate the removed fixture directory.
if _LOG:
    _SID = os.environ.setdefault("HOOK_PERF_AUDIT_SID", str(os.getsid(0)))
    if _SID != str(os.getsid(0)):
        _LOG = None
_STATE = {"opens": [], "bytes": 0, "stop_path": False, "stats": 0, "listings": {},
          "kinds": {}, "detached": 0, "lifted": []}
_FD_KIND = {}
_TRANSCRIPT = os.environ.get("HOOK_PERF_TRANSCRIPT") or None
_real_open = builtins.open
_real_io_open = io.open
_real_os_open = os.open
_real_os_read = os.read
_real_os_close = os.close
_real_listdir = os.listdir
_real_scandir = os.scandir
_real_stat = os.stat
_real_lstat = os.lstat


def _kind(path):
    """The record kind a path belongs to: a record directory and everything under it.

    ``recipient-inbox`` covers both derived per-recipient indexes (the dispatch
    ``recipient-inbox`` tree and ``peer-messages/index``).
    """
    if isinstance(path, int):
        return _FD_KIND.get(path, "other")
    try:
        text = os.fspath(path)
    except TypeError:
        return "other"
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    if text == "jobs.log" or text.endswith("/jobs.log"):
        return "jobs.log"
    if text.endswith("/session-tidy/sessions") or "/session-tidy/sessions/" in text:
        return "sessions-dir"
    if text.endswith("/peer-messages/pending") or "/peer-messages/pending/" in text:
        return "peer-pending"
    if text.endswith("/pending-delivery") or "/pending-delivery/" in text:
        return "pending-delivery"
    if ("/recipient-inbox/" in text or text.endswith("/recipient-inbox")
            or "/peer-messages/index/" in text or text.endswith("/peer-messages/index")):
        return "recipient-inbox"
    if (text == _TRANSCRIPT or text.endswith(".jsonl")
            and ("/.claude/projects/" in text or "/.codex/sessions/" in text)):
        return "transcript"
    return "other"


def _tally(kind, field, amount=1):
    bucket = _STATE["kinds"].get(kind)
    if bucket is None:
        bucket = _STATE["kinds"][kind] = {"opens": 0, "listings": 0, "stats": 0, "bytes": 0}
    bucket[field] += amount


def _size(value):
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    if isinstance(value, str):
        return len(value.encode("utf-8", "replace"))
    return 0


class _CountingFile:
    __slots__ = ("_file", "_kind")

    def __init__(self, file, kind="other"):
        self._file = file
        self._kind = kind

    def _count(self, amount):
        _STATE["bytes"] += amount
        _tally(self._kind, "bytes", amount)

    def __getattr__(self, name):
        return getattr(self._file, name)

    def __enter__(self):
        self._file.__enter__()
        return self

    def __exit__(self, *exc):
        return self._file.__exit__(*exc)

    def __iter__(self):
        for line in self._file:
            self._count(_size(line))
            yield line

    def __next__(self):
        line = next(self._file)
        self._count(_size(line))
        return line

    def read(self, *args):
        data = self._file.read(*args)
        self._count(_size(data))
        return data

    def readline(self, *args):
        data = self._file.readline(*args)
        self._count(_size(data))
        return data

    def readlines(self, *args):
        data = self._file.readlines(*args)
        self._count(sum(_size(item) for item in data))
        return data

    def readinto(self, buffer):
        count = self._file.readinto(buffer)
        if count:
            self._count(count)
        return count


def _record(path, mode):
    """Log one open and return its record kind.

    ``os.fdopen`` re-wraps a descriptor ``os.open`` already counted: it stays in
    the historical ``opens`` total but is not a second open of its record kind.
    """
    kind = _kind(path)
    if _LOG is None:
        return kind
    try:
        shown = os.fspath(path) if isinstance(path, (str, bytes, os.PathLike)) else str(path)
    except TypeError:
        shown = str(path)
    _STATE["opens"].append({"event": "open", "path": shown, "mode": str(mode)})
    if not isinstance(path, int):
        _tally(kind, "opens")
    return kind


def _open(file, mode="r", *args, **kwargs):
    kind = _record(file, mode)
    if isinstance(file, int):
        _FD_KIND.pop(file, None)  # the file object owns (and closes) it now
    return _CountingFile(_real_open(file, mode, *args, **kwargs), kind)


def _os_open(path, flags, *args, **kwargs):
    kind = _record(path, flags)
    fd = _real_os_open(path, flags, *args, **kwargs)
    _FD_KIND[fd] = kind
    return fd


def _os_read(fd, size):
    data = _real_os_read(fd, size)
    _STATE["bytes"] += _size(data)
    _tally(_FD_KIND.get(fd, "other"), "bytes", _size(data))
    return data


def _os_close(fd):
    _FD_KIND.pop(fd, None)
    return _real_os_close(fd)


def _listed(path):
    shown = "." if path is None else path
    if not isinstance(shown, int):
        try:
            shown = os.fspath(shown)
        except TypeError:
            shown = str(shown)
        if isinstance(shown, bytes):
            shown = shown.decode("utf-8", "replace")
    key = str(shown)
    _STATE["listings"][key] = _STATE["listings"].get(key, 0) + 1
    _tally(_kind(path if path is not None else "."), "listings")


def _os_listdir(path=None):
    _listed(path)
    return _real_listdir(path) if path is not None else _real_listdir()


def _os_scandir(path=None):
    _listed(path)
    return _real_scandir(path) if path is not None else _real_scandir()


def _os_stat(path, *args, **kwargs):
    _STATE["stats"] += 1
    _tally(_kind(path), "stats")
    return _real_stat(path, *args, **kwargs)


def _os_lstat(path, *args, **kwargs):
    _STATE["stats"] += 1
    _tally(_kind(path), "stats")
    return _real_lstat(path, *args, **kwargs)


if _LOG:
    builtins.open = _open
    io.open = _open
    os.open = _os_open
    os.read = _os_read
    os.close = _os_close
    # pathlib's iterdir/glob and os.walk resolve these at call time.
    os.listdir = _os_listdir
    os.scandir = _os_scandir
    os.stat = _os_stat
    os.lstat = _os_lstat

if os.environ.get("HOOK_PERF_SUPPRESS_DETACHED") == "1":
    # A detached child (its own session) is never the measured hook's work,
    # and it could outlive the fixture or write outside it. Keep the launch
    # (its arguments, descriptors and environment are still validated by the
    # real Popen) but run /bin/true instead of the child.
    import subprocess

    _RealPopen = subprocess.Popen

    class _DetachSuppressingPopen(_RealPopen):
        def __init__(self, args, *rest, **kwargs):
            if kwargs.get("start_new_session"):
                _STATE["detached"] += 1
                kwargs.pop("executable", None)
                args = "true" if kwargs.get("shell") else ["/bin/true"]
            super().__init__(args, *rest, **kwargs)

    subprocess.Popen = _DetachSuppressingPopen

if os.environ.get("HOOK_PERF_ALLOW_REPORT") == "1":
    try:
        root = os.environ["HOOK_PERF_CODE_ROOT"]
        sys.path.insert(0, root + "/tools")
        sys.path.insert(0, root + "/utilities")
        from fleet import herdr_projection
        herdr_projection.may_report = lambda *args, **kwargs: True
        _real_is_steward = herdr_projection.is_steward

        def _observed_is_steward(*args, **kwargs):
            _STATE["stop_path"] = True
            return _real_is_steward(*args, **kwargs)

        herdr_projection.is_steward = _observed_is_steward
    except Exception:
        pass
    try:
        sys.path.insert(0, os.environ["HOOK_PERF_CODE_ROOT"] + "/utilities")
        import peer_obligations
        peer_obligations.request_obligation_recovery = lambda **kwargs: False
    except Exception:
        pass

# A hook's time budget (a peer scan stops 2.5 s after spawn) would make what it
# reads depend on machine speed, and a cut-short scan would read as "fixed".
# Lift every budget a measured hook takes, so each scan runs to its end:
# * the peer module's scans run without a deadline, whoever passes one;
# * a hook script's budget constants are multiplied by 10**6 when its main()
#   starts (the hook reads them there). The trace function that does this is
#   installed only when the script names one, and removes itself at once.
# Every lift is recorded, so the test can tell a renamed budget from a lifted one.
_UNBOUNDED = {"peer-message.py": ("_pending_rows",),
              "peer-steward.py": ("undelivered_watch_rows",)}
_BUDGETS = [name for name in os.environ.get("HOOK_PERF_LIFT_BUDGETS", "").split(",") if name]
if _LOG and _BUDGETS:
    import importlib._bootstrap_external as _external

    _real_exec_module = _external._LoaderBasics.exec_module

    def _unbounded(function):
        def call(*args, **kwargs):
            if kwargs.get("deadline") is not None:
                kwargs["deadline"] = None
            return function(*args, **kwargs)
        call.__wrapped__ = function
        return call

    def _exec_module(self, module):
        _real_exec_module(self, module)
        filename = os.path.basename(getattr(module, "__file__", None) or "")
        for name in _UNBOUNDED.get(filename, ()):
            function = getattr(module, name, None)
            if callable(function):
                label = f"{filename}:{name}"
                setattr(module, name, _unbounded(function))
                if label not in _STATE["lifted"]:
                    _STATE["lifted"].append(label)

    _external._LoaderBasics.exec_module = _exec_module

    try:
        with _real_open(sys.argv[0], "r", encoding="utf-8") as _handle:
            _SOURCE = _handle.read()
    except (OSError, IndexError, ValueError, TypeError):
        _SOURCE = ""
    _NAMED = [name for name in _BUDGETS if name in _SOURCE]
    if _NAMED:
        def _lift_at_main(frame, event, arg):
            if frame.f_code.co_name == "main" and frame.f_globals.get("__name__") == "__main__":
                sys.settrace(None)
                for name in _NAMED:
                    if isinstance(frame.f_globals.get(name), (int, float)):
                        frame.f_globals[name] *= 10 ** 6
                        _STATE["lifted"].append("__main__:" + name)
            return None

        sys.settrace(_lift_at_main)


@atexit.register
def _flush():
    if _LOG is None:
        return
    try:
        with _real_open(_LOG, "a", encoding="utf-8") as handle:
            for event in _STATE["opens"]:
                handle.write(json.dumps(event, sort_keys=True) + "\n")
            busiest = sorted(_STATE["listings"].items(), key=lambda item: -item[1])[:50]
            handle.write(json.dumps({
                "event": "summary", "opens": len(_STATE["opens"]),
                "bytes": _STATE["bytes"], "stop_path": _STATE["stop_path"],
                "pid": os.getpid(), "stats": _STATE["stats"],
                "listings": sum(_STATE["listings"].values()),
                "listings_by_path": dict(busiest), "kinds": _STATE["kinds"],
                "detached": _STATE["detached"], "lifted": _STATE["lifted"],
            }, sort_keys=True) + "\n")
    except OSError:
        pass
'''

_HERDR = """#!/bin/sh
printf '%s\\n' "$*" >> "$HOOK_PERF_HERDR_LOG"
exit 0
"""


class FixtureCase:
    """One built fixture plus the scenario helpers every case shares."""

    def __init__(self, base: Path, scale: int, *, prime_via: str = "hook"):
        self.base = base
        self.base.mkdir(parents=True, exist_ok=True)
        self.fixture = fixture_module.build(self.base, scale=scale, prime_via=prime_via)
        self.fixture["counts"] = fixture_module.self_check(self.fixture)
        self.fixture["scale"] = scale
        self.dispatch_root = Path(self.fixture["dispatch_root"])
        self.records = pending.record_directory(self.dispatch_root, TARGET)
        self.predecessor_records = pending.record_directory(
            self.dispatch_root, fixture_module.PREDECESSOR)
        self.terminal_records = {}
        for path in sorted(self.predecessor_records.glob("delivery-*.json")):
            try:
                state = json.loads(path.read_text(encoding="utf-8")).get("state")
            except (OSError, ValueError):
                state = None
            if state is not None and state != "pending":
                self.terminal_records[path.name] = path
        self.inbox_path = inbox.inbox_path(self.dispatch_root, "claude", TARGET)
        self.edge_path = inbox.edge_generation_path(self.dispatch_root)
        self.pristine = {}
        for path in sorted(self.records.glob("delivery-*.json")):
            self.pristine[path.name] = (path.read_bytes(), path.stat().st_mtime_ns)
        self.records_mtime = {True: self.records.stat().st_mtime_ns}
        self.full_inbox = (self.inbox_path.read_bytes(),
                           self.inbox_path.stat().st_mtime_ns)
        self.edge = (self.edge_path.read_bytes(), self.edge_path.stat().st_mtime_ns) \
            if self.edge_path.is_file() else None
        self.empty_inbox = self._prime_empty_inbox()
        self.shim_dir = self.base / "shim"
        self.shim_dir.mkdir(exist_ok=True)
        (self.shim_dir / "sitecustomize.py").write_text(_SHIM, encoding="utf-8")
        self.bin_dir = self.base / "bin"
        self.bin_dir.mkdir(exist_ok=True)
        herdr = self.bin_dir / "herdr"
        herdr.write_text(_HERDR, encoding="utf-8")
        herdr.chmod(0o755)
        self.herdr_log = self.base / "herdr.jsonl"

    # -- target state -----------------------------------------------------
    def _write_target(self, present: bool, directory_mtime_ns: int | None = None) -> None:
        for path in list(self.records.glob("delivery-*")):
            path.unlink()
        for path in list(self.records.glob("delivery-*.lock")):
            path.unlink()
        if present:
            for name, (data, mtime_ns) in self.pristine.items():
                path = self.records / name
                path.write_bytes(data)
                os.utime(path, ns=(mtime_ns, mtime_ns))
        if directory_mtime_ns is not None:
            # Directory identity is part of inbox currentness; restore it too.
            os.utime(self.records, ns=(directory_mtime_ns, directory_mtime_ns))

    def _write_inbox(self, snapshot) -> None:
        data, mtime_ns = snapshot
        self.inbox_path.write_bytes(data)
        os.utime(self.inbox_path, ns=(mtime_ns, mtime_ns))

    def _write_edge(self) -> None:
        if self.edge is None:
            if self.edge_path.is_file():
                self.edge_path.unlink()
            return
        data, mtime_ns = self.edge
        self.edge_path.write_bytes(data)
        os.utime(self.edge_path, ns=(mtime_ns, mtime_ns))

    def restore(self, *, present: bool) -> None:
        """Restore the target's claimed records and its warm inbox/edge state."""
        directory_mtime = self.records_mtime.get(present)
        self._write_target(present, directory_mtime)
        if present:
            self.records_mtime[True] = self.records.stat().st_mtime_ns
        self._write_edge()
        self._write_inbox(self.full_inbox if present else self.empty_inbox)

    def _prime_empty_inbox(self):
        """A current inbox naming no records, built by the real prefill path."""
        self._write_target(False)
        self.records_mtime[False] = self.records.stat().st_mtime_ns
        self.inbox_path.unlink(missing_ok=True)
        saved = dict(os.environ)
        try:
            os.environ.update(self.fixture["env"])
            import dispatch_session_sweep as sweep
            sweep.addressed_records(self.dispatch_root, KIND, TARGET)
        finally:
            os.environ.clear()
            os.environ.update(saved)
        data = (self.inbox_path.read_bytes(), self.inbox_path.stat().st_mtime_ns)
        self._write_target(True, self.records_mtime[True])
        self._write_inbox(self.full_inbox)
        return data

    def runner_inbox(self):
        """Re-publish the target inbox through the real obligation-runner prefill."""
        self._write_target(True, self.records_mtime[True])
        self.inbox_path.unlink(missing_ok=True)
        saved = dict(os.environ)
        try:
            os.environ.update(self.fixture["env"])
            fixture_module.prime_inbox(self.fixture, via_runner=True)
        finally:
            os.environ.clear()
            os.environ.update(saved)
        data = (self.inbox_path.read_bytes(), self.inbox_path.stat().st_mtime_ns)
        payload = inbox.read_inbox(self.dispatch_root, "claude", TARGET)
        self._write_inbox(self.full_inbox)
        return data, payload

    # -- subprocess -------------------------------------------------------
    def env(self, audit_log: Path, *, interactive: bool = False) -> dict:
        env = dict(os.environ)
        for key in tuple(env):
            if key.startswith(("AGENT_", "HERDR_", "OPENCODE_", "CLAUDE_", "CODEX_",
                               "HARNESS_", "XDG_")):
                env.pop(key, None)
        env.update(self.fixture["env"])
        env.update({
            "AGENT_HOME": str(ROOT),
            "PATH": str(self.bin_dir) + os.pathsep + env.get("PATH", ""),
            "PYTHONPATH": str(self.shim_dir),
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOOK_PERF_AUDIT_LOG": str(audit_log),
            "HOOK_PERF_HERDR_LOG": str(self.herdr_log),
            "HOOK_PERF_CODE_ROOT": str(ROOT),
            "HOOK_PERF_ALLOW_REPORT": "1",
            "HOOK_PERF_TRANSCRIPT": self.fixture["transcript"],
            "HOOK_PERF_SUPPRESS_DETACHED": "1",
            "HOOK_PERF_LIFT_BUDGETS": ",".join(LIFTED_BUDGETS),
            "HERDR_PANE_ID": "wPerf:p1",
            "AGENT_DISPATCH_CHILD": "1",
        })
        if interactive:
            # Most user-path hooks are silent for a dispatch child; the
            # interactive session is the one whose turn they slow down.
            env.pop("AGENT_DISPATCH_CHILD")
        return env

    def run_hook(self, hook: Path, payload: dict, *, argv: tuple = (),
                 interactive: bool = False, cwd: Path | None = None) -> dict:
        audit_log = self.base / f"audit-{time.time_ns()}.jsonl"
        self.herdr_log.unlink(missing_ok=True)
        started = time.monotonic()
        completed = subprocess.run(
            [sys.executable, "-B", str(hook), *argv], cwd=str(cwd or ROOT),
            env=self.env(audit_log, interactive=interactive), input=json.dumps(payload),
            text=True, capture_output=True, timeout=120)
        seconds = time.monotonic() - started
        events, summary, summaries = [], {}, []
        try:
            for line in audit_log.read_text(encoding="utf-8").splitlines():
                value = json.loads(line)
                if value.get("event") == "summary":
                    summary = value
                    summaries.append(value)
                else:
                    events.append(value)
        except (OSError, ValueError):
            pass
        finally:
            audit_log.unlink(missing_ok=True)
        herdr_rows = []
        if self.herdr_log.is_file():
            herdr_rows = self.herdr_log.read_text(encoding="utf-8").splitlines()
        # `opens`/`bytes` stay the hook process's own (the last summary, as
        # before). The record-kind counts add every audited process of the
        # hook's session: a child the hook waits for slows the turn as much.
        kinds = {}
        for row in summaries:
            for kind, counts in (row.get("kinds") or {}).items():
                bucket = kinds.setdefault(kind, dict.fromkeys(KIND_FIELDS, 0))
                for field in KIND_FIELDS:
                    bucket[field] += int(counts.get(field, 0))
        listings_by_path = {}
        for row in summaries:
            for path, count in (row.get("listings_by_path") or {}).items():
                listings_by_path[path] = listings_by_path.get(path, 0) + count
        return {"seconds": seconds, "returncode": completed.returncode,
                "stderr": completed.stderr[-2000:], "opens": summary.get("opens", 0),
                "bytes": summary.get("bytes", 0), "stop_path": summary.get("stop_path", False),
                "open_events": events, "herdr": herdr_rows, "kinds": kinds,
                "stats": sum(int(row.get("stats", 0)) for row in summaries),
                "listings": sum(int(row.get("listings", 0)) for row in summaries),
                "listings_by_path": listings_by_path,
                "detached": sum(int(row.get("detached", 0)) for row in summaries),
                "lifted": sorted({item for row in summaries for item in row.get("lifted", ())}),
                "processes": len(summaries)}

    # -- cases ------------------------------------------------------------
    def prompt_case(self, name: str, *, present: bool, stale: bool) -> dict:
        samples = []
        for _ in range(RUNS):
            self.restore(present=present)
            if stale:
                inbox.bump_edge_generation(self.dispatch_root)
            sample = self.run_hook(PROMPT_HOOK, {
                "session_id": TARGET, "hook_event_name": "UserPromptSubmit"})
            states = []
            for path in sorted(self.records.glob("delivery-*.json")):
                try:
                    states.append(json.loads(path.read_text(encoding="utf-8")).get("state"))
                except (OSError, ValueError):
                    states.append(None)
            sample["delivered"] = sum(
                state in {"claimed", "acked", "sent-ambiguous"} for state in states)
            sample["acked"] = sum(state == "acked" for state in states)
            samples.append(sample)
        return _summarize(name, samples)

    def stop_case(self) -> dict:
        samples = []
        for _ in range(RUNS):
            self.restore(present=True)
            samples.append(self.run_hook(STOP_HOOK, {
                "session_id": TARGET, "hook_event_name": "Stop"}))
        return _summarize("stop", samples)

    def payload(self, event: str, **extra) -> dict:
        """A runtime-shaped payload for the target session in the fixture's work dir."""
        value = {"session_id": TARGET, "hook_event_name": event,
                 "cwd": self.fixture["work"], "transcript_path": self.fixture["transcript"]}
        if event in {"PreToolUse", "PostToolUse"}:
            value.update({"tool_name": "Bash",
                          "tool_input": {"command": "ls", "description": "List files"}})
        if event == "PostToolUse":
            value["tool_response"] = {"stdout": "", "stderr": "", "interrupted": False}
        elif event == "UserPromptSubmit":
            value["prompt"] = "perf prompt"
        value.update(extra)
        return value

    def hook_case(self, name: str, hook: Path, event: str, *, argv: tuple = (),
                  runs: int = USER_PATH_RUNS, **extra) -> dict:
        """One user-path hook as the interactive session runs it, from the work dir."""
        samples = [self.run_hook(hook, self.payload(event, **extra), argv=argv,
                                 interactive=True, cwd=Path(self.fixture["work"]))
                   for _ in range(runs)]
        return _summarize(name, samples)


def _median(values):
    return statistics.median(values) if values else 0.0


def _summarize(name: str, samples: list[dict]) -> dict:
    kinds = {}
    for kind in sorted({kind for sample in samples for kind in sample.get("kinds", {})}):
        kinds[kind] = {
            field: int(_median([sample.get("kinds", {}).get(kind, {}).get(field, 0)
                                for sample in samples]))
            for field in KIND_FIELDS}
    busiest = max(samples, key=lambda sample: sample.get("listings", 0), default={})
    return {
        "name": name,
        "kinds": kinds,
        "stats": int(_median([sample.get("stats", 0) for sample in samples])),
        "listings": int(_median([sample.get("listings", 0) for sample in samples])),
        "listings_by_path": busiest.get("listings_by_path", {}),
        "detached": int(_median([sample.get("detached", 0) for sample in samples])),
        "processes": int(_median([sample.get("processes", 0) for sample in samples])),
        "lifted": sorted(set.intersection(*(set(sample.get("lifted", ())) for sample in samples))
                         if samples else ()),
        "seconds": _median([sample["seconds"] for sample in samples]),
        "opens": int(_median([sample["opens"] for sample in samples])),
        "bytes": int(_median([sample["bytes"] for sample in samples])),
        "returncodes": sorted({sample["returncode"] for sample in samples}),
        "delivered": int(_median([sample.get("delivered", 0) for sample in samples])),
        "acked": int(_median([sample.get("acked", 0) for sample in samples])),
        "stop_path": all(sample["stop_path"] for sample in samples),
        "stderr": next((sample["stderr"] for sample in samples if sample["stderr"]), ""),
        "herdr": next((sample["herdr"] for sample in samples if sample["herdr"]), []),
        "open_events": next((sample.get("open_events", []) for sample in samples
                             if sample.get("open_events")), []),
    }


class BudgetGateTest(unittest.TestCase):
    """The gate itself must reject an over-budget sample."""

    def test_over_budget_sample_fails(self):
        with self.assertRaises(AssertionError):
            check_budget("prompt", PROMPT_LIMIT_S + 0.5, PROMPT_LIMIT_S)
        with self.assertRaises(AssertionError):
            check_budget("stop", STOP_LIMIT_S + 0.1, STOP_LIMIT_S)
        with self.assertRaises(AssertionError):
            check_scale_time("prompt", 1.0, 1.6)  # 1.5x bound
        with self.assertRaises(AssertionError):
            check_scale_time("prompt", 2.0, 2.6)  # +0.5 s bound
        with self.assertRaises(AssertionError):
            check_scale_opens(100, 111)
        with self.assertRaises(AssertionError):
            check_read_budget("prompt", 66_000, 10_000)
        check_budget("prompt", PROMPT_LIMIT_S - 0.1, PROMPT_LIMIT_S)
        check_scale_time("prompt", 1.0, 1.4)
        check_scale_opens(100, 110)
        check_read_budget("prompt", 65_000, 10_000)


class CountGateTest(unittest.TestCase):
    """The count rules must reject an over-budget sample, too."""

    def test_growth_with_unrelated_state_fails(self):
        # The post-#1184 asyncRewake run still read every ledger once.
        with self.assertRaises(AssertionError):
            check_kind_growth("rewake", {"sessions-dir": {"opens": 660, "listings": 4}},
                              {"sessions-dir": {"opens": 1320, "listings": 4}})
        # The pre-#1184 run listed the lineage once per registry row.
        with self.assertRaises(AssertionError):
            check_kind_growth("rewake", {"sessions-dir": {"opens": 1, "listings": 1600}},
                              {"sessions-dir": {"opens": 1, "listings": 3200}})
        # A kind that first appears at scale 2 counts from zero.
        with self.assertRaises(AssertionError):
            check_kind_growth("prompt", {}, {"peer-pending": {"opens": KIND_SLACK + 1}})
        check_kind_growth("prompt", {"peer-pending": {"opens": 3, "listings": 1}},
                          {"peer-pending": {"opens": 3 + KIND_SLACK, "listings": 1 + KIND_SLACK}})
        check_kind_growth("prompt", {}, {"other": {"opens": KIND_SLACK}})

    def test_ceilings_fail_a_per_row_reread(self):
        with self.assertRaises(AssertionError):
            check_kind_ceilings("prompt", {"jobs.log": {"opens": JOBS_OPEN_CEILING + 1}})
        with self.assertRaises(AssertionError):
            check_kind_ceilings("rewake", {"sessions-dir": {"listings": SESSIONS_LIST_CEILING + 1}})
        with self.assertRaises(AssertionError):
            check_kind_ceilings("rewake", {"sessions-dir": {"listings": 1600}})
        check_kind_ceilings("prompt", {"jobs.log": {"opens": JOBS_OPEN_CEILING},
                                       "sessions-dir": {"listings": SESSIONS_LIST_CEILING}})

    def test_stat_growth_fails(self):
        with self.assertRaises(AssertionError):
            check_scale_stats("rewake", 1000, 1201)
        with self.assertRaises(AssertionError):
            check_scale_stats("rewake", 22_690, 45_206)
        check_scale_stats("rewake", 1000, 1200)

    def test_check_counts_runs_every_rule(self):
        base = {"kinds": {"jobs.log": {"opens": 1}}, "stats": 100}
        for scaled in ({"kinds": {"jobs.log": {"opens": JOBS_OPEN_CEILING + 1}}, "stats": 100},
                       {"kinds": {"jobs.log": {"opens": 1 + KIND_SLACK + 1}}, "stats": 100},
                       {"kinds": {"jobs.log": {"opens": 1}}, "stats": 121}):
            with self.assertRaises(AssertionError):
                check_counts("case", base, scaled)
        check_counts("case", base, {"kinds": {"jobs.log": {"opens": 1}}, "stats": 120})


class RatchetTest(unittest.TestCase):
    """KNOWN_GROWTH only records today's growth; it never grants new growth."""

    BASE = {"kinds": {"sessions-dir": {"opens": 660, "listings": 4}}, "stats": 1000}
    GROWN = {"kinds": {"sessions-dir": {"opens": 1320, "listings": 4}}, "stats": 2000}
    STAT_ONLY = {"kinds": {"sessions-dir": {"opens": 660, "listings": 4}}, "stats": 2000}

    def test_the_table_names_real_cases_rules_and_evidence(self):
        cases = set(PROMPT_CASES) | {case for case, *_ in USER_PATH_CASES}
        self.assertEqual(len(cases), len(PROMPT_CASES) + len(USER_PATH_CASES), "duplicate case")
        self.assertLessEqual(set(KNOWN_GROWTH), cases, "an entry names no measured case")
        self.assertLessEqual(set(REQUIRED_MODULE_LIFTS), cases, "a lift names no measured case")
        for case, (rules, evidence) in KNOWN_GROWTH.items():
            self.assertTrue(rules, case)
            self.assertLessEqual(set(rules), set(GROWTH_RULES), case)
            # file:line - why it grows - which fix removes it
            self.assertRegex(evidence, r"^\S+\.py:\d+.* - .* - .*remove", case)

    def test_a_listed_case_must_still_break_its_rules(self):
        known = {"case": (("growth", "stats"), "a.py:1 - walks - fix")}
        check_ratchet("case", self.BASE, self.GROWN, known)
        with self.assertRaisesRegex(AssertionError, "delete its KNOWN_GROWTH entry"):
            check_ratchet("case", self.BASE, self.BASE, known)
        with self.assertRaisesRegex(AssertionError, "remove 'growth' from its KNOWN_GROWTH"):
            check_ratchet("case", self.BASE, self.STAT_ONLY, known)

    def test_a_listed_case_cannot_break_another_rule(self):
        known = {"case": (("stats",), "a.py:1 - walks - fix")}
        check_ratchet("case", self.BASE, self.STAT_ONLY, known)
        with self.assertRaisesRegex(AssertionError, "only for stats"):
            check_ratchet("case", self.BASE, self.GROWN, known)

    def test_an_unlisted_case_passes_every_rule(self):
        check_ratchet("case", self.BASE, self.BASE, {})
        with self.assertRaises(AssertionError):
            check_ratchet("case", self.BASE, self.GROWN, {})
        with self.assertRaises(AssertionError):
            check_ratchet("case", self.BASE, self.STAT_ONLY, {})

    def test_a_listed_case_cannot_grow_faster_than_the_state(self):
        known = {"case": (("growth", "stats"), "a.py:1 - walks - fix")}
        # A per-row ledger reopen in the rewake scan: rows x ledgers, ~4x.
        per_row = {"kinds": {"sessions-dir": {"opens": 4_224_000, "listings": 4}},
                   "stats": 4_000_000}
        base = {"kinds": {"sessions-dir": {"opens": 1_056_000, "listings": 4}},
                "stats": 1_000_000}
        with self.assertRaisesRegex(AssertionError, "faster than the unrelated state"):
            check_ratchet("case", base, per_row, known)
        with self.assertRaisesRegex(AssertionError, "faster than the unrelated state"):
            check_ratchet("case", self.BASE, dict(self.GROWN, stats=2201), known)
        check_ratchet("case", self.BASE, dict(self.GROWN, stats=2200), known)

    def test_a_lost_child_is_a_failed_measurement(self):
        known = {"case": (("growth", "stats"), "a.py:1 - walks - fix")}
        with self.assertRaisesRegex(AssertionError, "measurement failed, not fixed"):
            check_ratchet("case", dict(self.BASE, processes=2),
                          dict(self.BASE, processes=1), known)
        with self.assertRaisesRegex(AssertionError, "measurement failed"):
            check_ratchet("case", dict(self.BASE, processes=1),
                          dict(self.BASE, processes=2), {})

    def test_a_hook_budget_must_run_lifted(self):
        with tempfile.TemporaryDirectory(prefix="hook-lift-") as tmp:
            hook = Path(tmp) / "hook.py"
            hook.write_text("_HOOK_BUDGET_S = 2.5\n", encoding="utf-8")
            with self.assertRaisesRegex(AssertionError, "did not lift __main__:_HOOK_BUDGET_S"):
                check_lifts("case", hook, {"lifted": []})
            check_lifts("case", hook, {"lifted": ["__main__:_HOOK_BUDGET_S"]})

    def test_ceilings_bind_listed_cases_too(self):
        known = {"case": (("growth", "stats"), "a.py:1 - walks - fix")}
        # The pre-#1184 asyncRewake run: one lineage listing per registry row.
        before = {"kinds": {"sessions-dir": {"opens": 660, "listings": 1602}}, "stats": 1_101_340}
        after = {"kinds": {"sessions-dir": {"opens": 1320, "listings": 3202}}, "stats": 4_314_536}
        with self.assertRaisesRegex(AssertionError, "lineage listed 1602 times"):
            check_ratchet("case", before, after, known)
        jobs = {"kinds": {"jobs.log": {"opens": JOBS_OPEN_CEILING + 1}}, "stats": 2000}
        with self.assertRaisesRegex(AssertionError, "jobs.log opened"):
            check_ratchet("case", self.BASE, jobs, known)


class AuditShimCountTest(unittest.TestCase):
    """The shim attributes opens, listings, stats and bytes to record kinds."""

    _PROBE = r"""
import os, pathlib, subprocess, sys
root = pathlib.Path(sys.argv[1])
sessions = root / "xdg" / "hearting" / "session-tidy" / "sessions"
pending = root / "peer" / "peer-messages" / "pending"
jobs = root / "state" / "dispatch" / "jobs.log"
for _ in range(3):
    sorted(sessions.iterdir())              # os.listdir
list(pending.glob("*.json"))                # os.scandir
for path in sessions.iterdir():
    path.lstat()
jobs.stat()
fd = os.open(jobs, os.O_RDONLY)
with os.fdopen(fd, "r") as handle:          # one logical open of jobs.log
    handle.read()
(pending / "a.json").read_text()
subprocess.Popen([sys.executable, "-c", "open(sys.argv[1], 'w')", str(root / "detached")],
                 start_new_session=True).wait()
"""

    def test_kinds_listings_stats_and_detached_launches(self):
        with tempfile.TemporaryDirectory(prefix="hook-shim-") as tmp:
            base = Path(tmp)
            (base / "shim").mkdir()
            (base / "shim" / "sitecustomize.py").write_text(_SHIM, encoding="utf-8")
            sessions = base / "xdg" / "hearting" / "session-tidy" / "sessions"
            pending = base / "peer" / "peer-messages" / "pending"
            jobs = base / "state" / "dispatch" / "jobs.log"
            for directory in (sessions, pending, jobs.parent):
                directory.mkdir(parents=True)
            for index in range(4):
                (sessions / f"s{index}.jsonl").write_text("{}\n", encoding="utf-8")
            (pending / "a.json").write_text("{}", encoding="utf-8")
            jobs.write_text("x" * 100, encoding="utf-8")
            (base / "probe.py").write_text(self._PROBE, encoding="utf-8")
            log = base / "audit.jsonl"
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("HOOK_PERF_")}
            env.update({"PYTHONPATH": str(base / "shim"), "HOOK_PERF_AUDIT_LOG": str(log),
                        "HOOK_PERF_SUPPRESS_DETACHED": "1"})
            subprocess.run([sys.executable, str(base / "probe.py"), str(base)],
                           env=env, check=True, timeout=60)
            summary = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
                       if json.loads(line).get("event") == "summary"][-1]
            kinds = summary["kinds"]
            self.assertEqual(kinds["sessions-dir"]["listings"], 4)
            self.assertEqual(kinds["peer-pending"]["listings"], 1)
            self.assertEqual(kinds["sessions-dir"]["stats"], 4)
            self.assertEqual(kinds["jobs.log"]["opens"], 1)
            self.assertEqual(kinds["jobs.log"]["bytes"], 100)
            self.assertGreaterEqual(kinds["jobs.log"]["stats"], 1)
            self.assertEqual(kinds["peer-pending"]["opens"], 1)
            self.assertGreaterEqual(summary["stats"], 5)
            self.assertEqual(summary["listings"], 5)
            self.assertEqual(summary["listings_by_path"][str(sessions)], 4)
            self.assertEqual(summary["detached"], 1)
            self.assertFalse((base / "detached").exists(), "a detached child really ran")


class AuditShimLiftTest(unittest.TestCase):
    """The shim lifts a hook's budget constant and the peer module's scan deadline."""

    _HOOK = r"""
import importlib.util, json, sys, time
_HOOK_BUDGET_S = 2.5
_UNRELATED_S = 2.5


def main():
    spec = importlib.util.spec_from_file_location("_peer", sys.argv[1])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    print(json.dumps({"budget": _HOOK_BUDGET_S, "unrelated": _UNRELATED_S,
                      "deadline": module._pending_rows(deadline=time.monotonic() + 2.5)}))


if __name__ == "__main__":
    main()
"""

    def test_budget_constant_and_scan_deadline_are_lifted(self):
        with tempfile.TemporaryDirectory(prefix="hook-lift-") as tmp:
            base = Path(tmp)
            (base / "shim").mkdir()
            (base / "shim" / "sitecustomize.py").write_text(_SHIM, encoding="utf-8")
            (base / "hook.py").write_text(self._HOOK, encoding="utf-8")
            (base / "peer-message.py").write_text(
                "def _pending_rows(*, deadline=None):\n    return deadline\n", encoding="utf-8")
            log = base / "audit.jsonl"
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("HOOK_PERF_")}
            env.update({"PYTHONPATH": str(base / "shim"), "HOOK_PERF_AUDIT_LOG": str(log),
                        "HOOK_PERF_LIFT_BUDGETS": ",".join(LIFTED_BUDGETS)})
            done = subprocess.run([sys.executable, str(base / "hook.py"),
                                   str(base / "peer-message.py")],
                                  env=env, check=True, timeout=60, capture_output=True, text=True)
            value = json.loads(done.stdout)
            self.assertEqual(value, {"budget": 2.5e6, "unrelated": 2.5, "deadline": None})
            summary = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
                       if json.loads(line).get("event") == "summary"][-1]
            self.assertEqual(sorted(summary["lifted"]),
                             ["__main__:_HOOK_BUDGET_S", "peer-message.py:_pending_rows"])


class AuditShimScopeTest(unittest.TestCase):
    """The audit log belongs to the measured hook's session, not to daemons it detaches."""

    def _run(self, start_new_session: bool) -> list[dict]:
        with tempfile.TemporaryDirectory(prefix="hook-shim-") as tmp:
            base = Path(tmp)
            (base / "shim").mkdir()
            (base / "shim" / "sitecustomize.py").write_text(_SHIM, encoding="utf-8")
            log = base / "audit.jsonl"
            child = ("import subprocess, sys; subprocess.run([sys.executable, '-c', "
                     "\"open('/dev/null').close()\"], start_new_session=%r, check=True)" % start_new_session)
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("HOOK_PERF_")}
            env.update({"PYTHONPATH": str(base / "shim"), "HOOK_PERF_AUDIT_LOG": str(log)})
            subprocess.run([sys.executable, "-c", child], env=env, check=True, timeout=60)
            return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def test_a_detached_child_writes_no_audit(self):
        rows = self._run(start_new_session=True)
        self.assertEqual(sum(row.get("event") == "summary" for row in rows), 1)
        self.assertNotIn("/dev/null", {row.get("path") for row in rows})

    def test_a_child_in_the_hook_session_is_still_audited(self):
        rows = self._run(start_new_session=False)
        self.assertEqual(sum(row.get("event") == "summary" for row in rows), 2)
        self.assertIn("/dev/null", {row.get("path") for row in rows})


def _count_table(rows) -> str:
    """Per hook and record kind: opens/listings at scale 1 and 2, plus stat totals."""
    lines = ["case                | scale | " + " | ".join(f"{kind:>16}" for kind in RECORD_KINDS)
             + " |  stats | jobs.log MB",
             "--------------------+-------+-" + "-+-".join("-" * 16 for _ in RECORD_KINDS)
             + "-+--------+-----------"]
    for case_name, run1, run2 in rows:
        for scale, run in ((1, run1), (2, run2)):
            cells = []
            for kind in RECORD_KINDS:
                counts = run["kinds"].get(kind, {})
                cells.append(f"{counts.get('opens', 0):>7}o/{counts.get('listings', 0):>6}l")
            jobs_mb = run["kinds"].get("jobs.log", {}).get("bytes", 0) / 1e6
            lines.append(f"{case_name:<19} | {scale:5d} | " + " | ".join(cells)
                         + f" | {run['stats']:6d} | {jobs_mb:9.1f}")
    return "\n".join(lines)


class HookPerfBudgetTest(unittest.TestCase):
    """d1/d2: the real entrypoints on fixed state at scale 1 and 2.

    Both fixtures are built once for the class; every case restores or does
    not depend on the target state the other cases leave behind.
    """

    @classmethod
    def setUpClass(cls):
        if os.environ.get("HOOK_PERF_SKIP_BUDGET") == "1":
            raise unittest.SkipTest("HOOK_PERF_SKIP_BUDGET=1")
        cls._tmp = tempfile.TemporaryDirectory(prefix="hook-budget-")
        base = Path(cls._tmp.name)
        try:
            started = time.monotonic()
            cls.scale1 = FixtureCase(base / "scale1", 1)
            cls.scale2 = FixtureCase(base / "scale2", 2)
            cls.build_seconds = time.monotonic() - started
        except BaseException:
            cls._tmp.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_prompt_and_stop_keep_their_budget_and_scale(self):
        scale1, scale2 = self.scale1, self.scale2
        # Target share and the registry are fixed across scales.
        self.assertEqual(len(scale1.pristine), fixture_module.TARGET_RECORDS)
        self.assertEqual(len(scale2.pristine), fixture_module.TARGET_RECORDS)
        self.assertEqual(scale1.fixture["counts"]["jobs_rows"],
                         scale2.fixture["counts"]["jobs_rows"])
        self.assertEqual(scale1.fixture["counts"]["jobs_bytes"],
                         scale2.fixture["counts"]["jobs_bytes"])

        # Measure every case first, so a failure still prints both tables.
        rows = [
            ("prompt-empty",
             scale1.prompt_case("prompt-empty", present=False, stale=False),
             scale2.prompt_case("prompt-empty", present=False, stale=False)),
            ("prompt-two",
             scale1.prompt_case("prompt-two", present=True, stale=False),
             scale2.prompt_case("prompt-two", present=True, stale=False)),
            ("prompt-stale",
             scale1.prompt_case("prompt-stale", present=True, stale=True),
             scale2.prompt_case("prompt-stale", present=True, stale=True)),
            ("stop", scale1.stop_case(), scale2.stop_case()),
        ]
        # The steady-state installed path: the obligation runner publishes
        # the inbox, with terminal records under the same granted scope.
        # The hook must follow only the named open records and never open
        # a terminal one.
        for scale, case in ((1, scale1), (2, scale2)):
            snapshot, payload = case.runner_inbox()
            self.assertIsNotNone(
                payload, f"scale {scale}: runner prefill wrote no inbox")
            self.assertTrue(inbox.is_current(payload, case.dispatch_root),
                            f"scale {scale}: runner inbox is not current")
            terminal_ids = set(case.fixture["terminal_delivery_ids"])
            named = {item["delivery_id"] for item in payload["delivery_ids"]}
            self.assertTrue(terminal_ids, f"scale {scale}: no terminal fixture record")
            self.assertFalse(
                terminal_ids & named,
                f"scale {scale}: runner inbox named a terminal record")
            self.assertEqual(
                sorted(named), sorted(path.stem for path in case.records.glob("delivery-*.json")),
                f"scale {scale}: runner inbox did not name exactly the open records")
            case.full_inbox = snapshot
        rows.append(("prompt-runner",
                     scale1.prompt_case("prompt-runner", present=True, stale=False),
                     scale2.prompt_case("prompt-runner", present=True, stale=False)))
        self.assertEqual(tuple(case_name for case_name, _, _ in rows), PROMPT_CASES)

        print(f"\nfixture build (both scales): {self.build_seconds:.1f}s")
        print("scale | case          | median s | opens | bytes    | stop_path")
        print("------+---------------+----------+-------+----------+----------")
        for case_name, run1, run2 in rows:
            for scale, run in ((1, run1), (2, run2)):
                print(f"{scale:5d} | {case_name:<13} | {run['seconds']:8.3f} | "
                      f"{run['opens']:5d} | {run['bytes']:8d} | {run['stop_path']}")
        print("")
        print(_count_table(rows))
        print("")

        # The same runs, judged by counts: per record kind, nothing grows with
        # unrelated state and nothing re-reads the registry or re-lists the
        # lineage per row.
        for case_name, run1, run2 in rows:
            hook = STOP_HOOK if case_name == "stop" else PROMPT_HOOK
            with self.subTest(case=case_name, rule="budgets"):
                required = REQUIRED_MODULE_LIFTS.get(case_name, ())
                check_lifts(case_name, hook, run1, required)
                check_lifts(case_name, hook, run2, required)
            with self.subTest(case=case_name, rule="counts"):
                check_ratchet(case_name, run1, run2)

        for case_name, run1, run2 in rows:
            self.assertEqual(run1["returncodes"], [0], run1)
            self.assertEqual(run2["returncodes"], [0], run2)
            limit = STOP_LIMIT_S if case_name == "stop" else PROMPT_LIMIT_S
            check_budget(case_name, run1["seconds"], limit)
            # Growth with unrelated state is judged by the count rules above (per-kind
            # growth, stat growth, the 2.2x cap for listed cases). The wall-clock delta
            # follows machine load (+0.31/+0.35/+0.50 s across runs on 11-04), so it is
            # reported, not asserted; the absolute budgets stay asserted.
            try:
                check_scale_time(case_name, run1["seconds"], run2["seconds"])
            except AssertionError as exc:
                print(f"scale-time (informational): {exc}", file=sys.stderr)
            check_scale_opens(run1["opens"], run2["opens"])
            self.assertGreater(run1["opens"], 0, f"{case_name}: audit shim saw no open")
            if case_name in {"prompt-two", "prompt-stale", "prompt-runner"}:
                self.assertEqual(run1["delivered"], 2,
                                 f"{case_name}: both open notices must be delivered")
                self.assertEqual(run2["delivered"], 2,
                                 f"{case_name}: both open notices must be delivered")
            if case_name == "stop":
                self.assertTrue(run1["stop_path"],
                                "Stop sample never reached the header path")
                self.assertTrue(run2["stop_path"],
                                "Stop sample never reached the header path")
                self.assertTrue(any("report-metadata" in row for row in run1["herdr"]),
                                f"fake herdr saw no metadata report: {run1['herdr']}")
                self.assertTrue(any("--title" in row for row in run1["herdr"]),
                                f"fake herdr saw no header title: {run1['herdr']}")
            if case_name == "prompt-two":
                jobs_bytes = scale1.fixture["counts"]["jobs_bytes"]
                check_read_budget(case_name, run1["bytes"], jobs_bytes)
                check_read_budget(case_name, run2["bytes"], jobs_bytes)
            if case_name == "prompt-runner":
                terminal_names = set(scale1.terminal_records)
                opened_names = {Path(event["path"]).name for event in run1["open_events"]
                                if isinstance(event, dict) and event.get("event") == "open"
                                and isinstance(event.get("path"), str)}
                self.assertTrue(terminal_names, "fixture has no terminal record to exclude")
                self.assertFalse(
                    terminal_names & opened_names,
                    "the prompt opened a terminal record named by the runner inbox")

    def test_user_path_hooks_keep_their_record_counts(self):
        """Every Python hook a user's prompt, tool call or turn end starts (USER_PATH_CASES)."""
        rows = []
        for case_name, hook, event, argv in USER_PATH_CASES:
            extra = {"turn_id": "perf-turn"} if case_name.startswith("codex-") else {}
            run1 = self.scale1.hook_case(case_name, hook, event, argv=argv, **extra)
            run2 = self.scale2.hook_case(case_name, hook, event, argv=argv, **extra)
            rows.append((case_name, run1, run2))
        print("")
        print(_count_table(rows))
        print("")
        hooks = {case_name: hook for case_name, hook, _event, _argv in USER_PATH_CASES}
        for case_name, run1, run2 in rows:
            with self.subTest(case=case_name, rule="exit"):
                for run in (run1, run2):
                    self.assertEqual(run["returncodes"], [0], f"{case_name}: {run['stderr']}")
                    self.assertGreater(run["processes"], 0,
                                       f"{case_name}: audit shim saw no process")
            with self.subTest(case=case_name, rule="budgets"):
                required = REQUIRED_MODULE_LIFTS.get(case_name, ())
                check_lifts(case_name, hooks[case_name], run1, required)
                check_lifts(case_name, hooks[case_name], run2, required)
            with self.subTest(case=case_name, rule="counts"):
                check_ratchet(case_name, run1, run2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
