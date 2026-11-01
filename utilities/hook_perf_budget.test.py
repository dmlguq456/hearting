#!/usr/bin/env python3
"""d1/d2 performance regression for the prompt and Stop hooks.

The fixture (``hook_perf_fixture.py``) holds far more unrelated global state
than the live tree: sessions ledgers, handover snapshots, steward markers,
deliveries, peer history and a >= 49 MiB registry. Only the target session's
own share and the registry stay fixed when ``scale`` doubles.

Every measured hook runs as a real subprocess with a test-only ``sitecustomize``
shim. The shim records every ``open``/``os.open``/``io.open`` and every byte
read to a JSONL sidecar, injects ``may_report=True`` (the product has no test
flag), records that the Stop header path reached ``is_steward``, and stops the
detached obligation runner so no background process mutates the fixture while a
sample is measured. A fake ``herdr`` executable records the argv it receives.

Assertions: prompt <= 3 s and Stop <= 2 s at scale 1; scale 2 may be at most
1.5x and +0.5 s over scale 1 with at most 1.1x opened files; a delivered prompt
pass reads at most 6.5x the fixed registry size (the pre-fix path read it 13
times); that ratio, not a wall-clock constant from one machine, carries the
"faster than before" claim. ``check_budget``/``check_read_budget`` themselves are
unit-tested against over-budget samples so the gate cannot pass vacuously.
"""
from __future__ import annotations

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
_STATE = {"opens": [], "bytes": 0, "stop_path": False}
_real_open = builtins.open
_real_io_open = io.open
_real_os_open = os.open
_real_os_read = os.read


def _size(value):
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    if isinstance(value, str):
        return len(value.encode("utf-8", "replace"))
    return 0


class _CountingFile:
    __slots__ = ("_file",)

    def __init__(self, file):
        self._file = file

    def __getattr__(self, name):
        return getattr(self._file, name)

    def __enter__(self):
        self._file.__enter__()
        return self

    def __exit__(self, *exc):
        return self._file.__exit__(*exc)

    def __iter__(self):
        for line in self._file:
            _STATE["bytes"] += _size(line)
            yield line

    def __next__(self):
        line = next(self._file)
        _STATE["bytes"] += _size(line)
        return line

    def read(self, *args):
        data = self._file.read(*args)
        _STATE["bytes"] += _size(data)
        return data

    def readline(self, *args):
        data = self._file.readline(*args)
        _STATE["bytes"] += _size(data)
        return data

    def readlines(self, *args):
        data = self._file.readlines(*args)
        _STATE["bytes"] += sum(_size(item) for item in data)
        return data

    def readinto(self, buffer):
        count = self._file.readinto(buffer)
        if count:
            _STATE["bytes"] += count
        return count


def _record(path, mode):
    if _LOG is None:
        return
    try:
        shown = os.fspath(path) if isinstance(path, (str, bytes, os.PathLike)) else str(path)
    except TypeError:
        shown = str(path)
    _STATE["opens"].append({"event": "open", "path": shown, "mode": str(mode)})


def _open(file, mode="r", *args, **kwargs):
    _record(file, mode)
    return _CountingFile(_real_open(file, mode, *args, **kwargs))


def _os_open(path, flags, *args, **kwargs):
    _record(path, flags)
    return _real_os_open(path, flags, *args, **kwargs)


def _os_read(fd, size):
    data = _real_os_read(fd, size)
    _STATE["bytes"] += _size(data)
    return data


if _LOG:
    builtins.open = _open
    io.open = _open
    os.open = _os_open
    os.read = _os_read

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


@atexit.register
def _flush():
    if _LOG is None:
        return
    try:
        with _real_open(_LOG, "a", encoding="utf-8") as handle:
            for event in _STATE["opens"]:
                handle.write(json.dumps(event, sort_keys=True) + "\n")
            handle.write(json.dumps({
                "event": "summary", "opens": len(_STATE["opens"]),
                "bytes": _STATE["bytes"], "stop_path": _STATE["stop_path"],
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
    def env(self, audit_log: Path) -> dict:
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
            "HERDR_PANE_ID": "wPerf:p1",
            "AGENT_DISPATCH_CHILD": "1",
        })
        return env

    def run_hook(self, hook: Path, payload: dict) -> dict:
        audit_log = self.base / f"audit-{time.time_ns()}.jsonl"
        self.herdr_log.unlink(missing_ok=True)
        started = time.monotonic()
        completed = subprocess.run(
            [sys.executable, "-B", str(hook)], cwd=str(ROOT),
            env=self.env(audit_log), input=json.dumps(payload), text=True,
            capture_output=True, timeout=120)
        seconds = time.monotonic() - started
        events, summary = [], {}
        try:
            for line in audit_log.read_text(encoding="utf-8").splitlines():
                value = json.loads(line)
                if value.get("event") == "summary":
                    summary = value
                else:
                    events.append(value)
        except (OSError, ValueError):
            pass
        finally:
            audit_log.unlink(missing_ok=True)
        herdr_rows = []
        if self.herdr_log.is_file():
            herdr_rows = self.herdr_log.read_text(encoding="utf-8").splitlines()
        return {"seconds": seconds, "returncode": completed.returncode,
                "stderr": completed.stderr[-2000:], "opens": summary.get("opens", 0),
                "bytes": summary.get("bytes", 0), "stop_path": summary.get("stop_path", False),
                "open_events": events, "herdr": herdr_rows}

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


def _median(values):
    return statistics.median(values) if values else 0.0


def _summarize(name: str, samples: list[dict]) -> dict:
    return {
        "name": name,
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


class HookPerfBudgetTest(unittest.TestCase):
    """d1/d2: the real entrypoints on fixed state at scale 1 and 2."""

    def test_prompt_and_stop_keep_their_budget_and_scale(self):
        if os.environ.get("HOOK_PERF_SKIP_BUDGET") == "1":
            self.skipTest("HOOK_PERF_SKIP_BUDGET=1")
        with tempfile.TemporaryDirectory(prefix="hook-budget-") as tmp:
            base = Path(tmp)
            scale1 = FixtureCase(base / "scale1", 1)
            scale2 = FixtureCase(base / "scale2", 2)
            # Target share and the registry are fixed across scales.
            self.assertEqual(len(scale1.pristine), fixture_module.TARGET_RECORDS)
            self.assertEqual(len(scale2.pristine), fixture_module.TARGET_RECORDS)
            self.assertEqual(scale1.fixture["counts"]["jobs_rows"],
                             scale2.fixture["counts"]["jobs_rows"])
            self.assertEqual(scale1.fixture["counts"]["jobs_bytes"],
                             scale2.fixture["counts"]["jobs_bytes"])
            self.assertEqual(len(scale1.pristine), fixture_module.TARGET_RECORDS)
            self.assertEqual(len(scale2.pristine), fixture_module.TARGET_RECORDS)

            rows = []
            for case_name, run1, run2 in (
                    ("prompt-empty",
                     scale1.prompt_case("prompt-empty", present=False, stale=False),
                     scale2.prompt_case("prompt-empty", present=False, stale=False)),
                    ("prompt-two",
                     scale1.prompt_case("prompt-two", present=True, stale=False),
                     scale2.prompt_case("prompt-two", present=True, stale=False)),
                    ("prompt-stale",
                     scale1.prompt_case("prompt-stale", present=True, stale=True),
                     scale2.prompt_case("prompt-stale", present=True, stale=True)),
                    ("stop", scale1.stop_case(), scale2.stop_case())):
                self.assertEqual(run1["returncodes"], [0], run1)
                self.assertEqual(run2["returncodes"], [0], run2)
                limit = STOP_LIMIT_S if case_name == "stop" else PROMPT_LIMIT_S
                check_budget(case_name, run1["seconds"], limit)
                check_scale_time(case_name, run1["seconds"], run2["seconds"])
                check_scale_opens(run1["opens"], run2["opens"])
                self.assertGreater(run1["opens"], 0, f"{case_name}: audit shim saw no open")
                if case_name in {"prompt-two", "prompt-stale"}:
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
                rows.append((case_name, run1, run2))
                if case_name == "prompt-two":
                    jobs_bytes = scale1.fixture["counts"]["jobs_bytes"]
                    check_read_budget(case_name, run1["bytes"], jobs_bytes)
                    check_read_budget(case_name, run2["bytes"], jobs_bytes)

            print("\nscale | case          | median s | opens | bytes    | stop_path")
            print("------+---------------+----------+-------+----------+----------")
            for case_name, run1, run2 in rows:
                for scale, run in ((1, run1), (2, run2)):
                    print(f"{scale:5d} | {case_name:<13} | {run['seconds']:8.3f} | "
                          f"{run['opens']:5d} | {run['bytes']:8d} | {run['stop_path']}")
            print("")

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
            run1 = scale1.prompt_case("prompt-runner", present=True, stale=False)
            run2 = scale2.prompt_case("prompt-runner", present=True, stale=False)
            for run in (run1, run2):
                self.assertEqual(run["returncodes"], [0], run)
            check_budget("prompt-runner", run1["seconds"], PROMPT_LIMIT_S)
            check_scale_time("prompt-runner", run1["seconds"], run2["seconds"])
            check_scale_opens(run1["opens"], run2["opens"])
            self.assertEqual(run1["delivered"], 2, "prompt-runner: both open notices must be delivered")
            self.assertEqual(run2["delivered"], 2, "prompt-runner: both open notices must be delivered")
            terminal_names = set(scale1.terminal_records)
            opened_names = {Path(event["path"]).name for event in run1["open_events"]
                            if isinstance(event, dict) and event.get("event") == "open"
                            and isinstance(event.get("path"), str)}
            self.assertTrue(terminal_names, "fixture has no terminal record to exclude")
            self.assertFalse(
                terminal_names & opened_names,
                "the prompt opened a terminal record named by the runner inbox")
            rows.append(("prompt-runner", run1, run2))
            for scale, run in ((1, run1), (2, run2)):
                print(f"{scale:5d} | {'prompt-runner':<13} | {run['seconds']:8.3f} | "
                      f"{run['opens']:5d} | {run['bytes']:8d} | {run['stop_path']}")
            print("")


if __name__ == "__main__":
    unittest.main(verbosity=2)
