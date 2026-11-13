#!/usr/bin/env python3
"""Incremental paths against a fresh full computation, event by event.

The reuse in the registry reader, the stream readers, the Fleet collectors and
the answer-wait producer must never answer with a value that a process starting
from nothing would not give for the same inputs.  Every scenario below changes
one input the way a real writer does, then compares

* WARM  - one long-lived process that keeps all of its caches between steps, and
* FRESH - a new subprocess with empty caches (and no disk cache), same fixture,
          same injected clock, same function, and
* RESUME - a new subprocess that finds the disk cursor the warm process left behind
          (E24..E27 only), same fixture, same injected clock.

WARM also cuts the registry into small segments (``_PARSE_CHUNK_LINES``) while FRESH
uses the default, so "a different way of splitting the work gives the same answer"
is compared on every event.

The comparison is exact except for the small allow-list ``ALLOWED``.  There is
no timing assertion in this file; speed is measured by
``tools/measure_e2e_latency.py`` and is never a gate here.

A failure message starts with the event id (``E01``..``E29``) and the JSON
pointer of the first difference, so a falsifier run can tell which event caught
it.  Weakening a scenario or widening ``ALLOWED`` to make a stale value pass is
the thing this file exists to prevent.
"""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import collections.abc
import contextlib
import datetime as _datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time as _time
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
for _entry in (ROOT, ROOT / "utilities", ROOT / "tools"):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

# ---------------------------------------------------------------------------
# injected clock
# ---------------------------------------------------------------------------

CLOCK_BASE = 1_800_000_000.0          # 2027-01-15T08:00:00Z, later than any real mtime
WARM_CHUNK_LINES = 3                  # the warm process cuts the registry into 3-row segments
FLEET_PACKAGES = ("fleet", "tools.fleet")   # the producer imports the second spelling

# Modules that bind the wall clock by name.  ``.now()`` / ``strftime`` readers
# that import the stdlib names inside a function are covered by the global
# ``time`` patches below.
CLOCK_DATETIME_MODULES = tuple(
    "%s.collectors.%s" % (pkg, name)
    for pkg in FLEET_PACKAGES for name in ("memory", "dispatch", "shell_jobs")
) + ("route_authority",)


class FixtureClock:
    """One settable instant for every wall-clock reader the collectors use.

    ``time.monotonic`` is deliberately left real: it only measures durations,
    and pinning it would hide the 30 s observation-gap rules in the registry
    reader instead of exercising them.
    """

    def __init__(self, now: float = CLOCK_BASE):
        self.now = float(now)

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)

    def time(self) -> float:
        return self.now

    def time_ns(self) -> int:
        return int(round(self.now * 1_000_000_000))

    def frozen_datetime(self):
        clock = self

        class FrozenDatetime(_datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromtimestamp(clock.now, tz)

            @classmethod
            def utcnow(cls):
                return cls.fromtimestamp(clock.now, _datetime.timezone.utc).replace(tzinfo=None)

        return FrozenDatetime

    @contextlib.contextmanager
    def installed(self):
        frozen = self.frozen_datetime()
        shim = types.SimpleNamespace(**{name: getattr(_datetime, name) for name in dir(_datetime)
                                        if not name.startswith("__")})
        shim.datetime = frozen
        real_localtime, real_gmtime = _time.localtime, _time.gmtime
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("time.time", self.time))
            stack.enter_context(mock.patch("time.time_ns", self.time_ns))
            stack.enter_context(mock.patch(
                "time.localtime", lambda secs=None: real_localtime(self.now if secs is None else secs)))
            stack.enter_context(mock.patch(
                "time.gmtime", lambda secs=None: real_gmtime(self.now if secs is None else secs)))
            for name in CLOCK_DATETIME_MODULES:
                module = sys.modules.get(name)
                held = getattr(module, "datetime", None)
                if held is _datetime.datetime:
                    stack.enter_context(mock.patch.object(module, "datetime", frozen))
                elif held is _datetime:
                    stack.enter_context(mock.patch.object(module, "datetime", shim))
            yield self


# ---------------------------------------------------------------------------
# the allow-list: the only thing the comparison ignores
# ---------------------------------------------------------------------------
# Entries come from "same inputs, same clock, two fresh processes" (self-check A)
# and nowhere else.  Every entry carries its reason.  It is empty on purpose when
# self-check A shows nothing to allow: an entry is a hole in the comparison.
# Run metadata that is not part of any observed value is stripped by
# ``normalize`` below (fixture roots), not listed here.

ALLOWED: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"^/_meta(/|$)"),
     "how the computing process obtained its values (for example how many Codex cursors it "
     "continued from disk); it is read by the self-checks of E24..E27, never an observed value"),
    (re.compile(r"^/o3_fleet/(fast_first|full)/observations/[^/]+/(last_success_at|age)$"),
     "health bookkeeping stamped with time.monotonic(): relative to the process start "
     "(a warm process by definition has an older first success than a new one); "
     "the state and reason beside them stay compared"),
)

# Run metadata that names the computing process itself.  A rewrite keeps the rest
# of the string (here the injected clock) in the comparison; it never drops a value.
RUN_METADATA_REWRITES: tuple[tuple[re.Pattern, str, str], ...] = (
    (re.compile(r"^fleet:\d+:(\d+)$"), r"fleet:$PID:\1",
     "observations.*.generation_identity is 'fleet:<computing pid>:<clock ns>'; "
     "the pid is the one value that differs between any two processes"),
)


# ---------------------------------------------------------------------------
# fixture
# ---------------------------------------------------------------------------

CLAUDE_PID, CODEX_PID = 910_001, 910_002
CLAUDE_SID = "11111111-1111-4111-8111-111111111111"
CODEX_SID = "22222222-2222-4222-8222-222222222222"
OTHER_SID = "33333333-3333-4333-8333-333333333333"
SUCCESSOR_SID = "44444444-4444-4444-8444-444444444444"
SECOND_CODEX_SID = "44444444-4444-4444-8444-444444444444"


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def iso(epoch: float) -> str:
    return _datetime.datetime.fromtimestamp(epoch, _datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def touch(path: Path, when: float) -> None:
    os.utime(path, (when, when))


def route_record(root: Path, index: int, *, revision: int = 0, nodes: int = 2) -> dict:
    """A sealed route record; ``revision`` changes the content and so the hash."""
    from route_identity import route_hash, route_id_from_hash
    record = {
        "schema_version": 1, "capability": "autopilot-code", "capability_mode": "dev",
        "requested_intensity": "standard", "effective_intensity": "standard",
        "execution_topology": "staged", "owner_depth": 1, "tracking": "tracked",
        "spec_touch": False, "cwd": str(root / "work"), "artifact_root": str(root / "artifacts"),
        "fixture_route": index, "fixture_revision": revision, "slug": "route-slug-%d" % index,
        "nodes": [{"id": "n%d" % n, "kind": "pipeline-stage", "depth": 2,
                   "depends_on": ["n%d" % (n - 1)] if n else [], "role": "fast implementer",
                   "write_scope": ["src/**"], "resource_class": "normal",
                   "completion_gate": "gate-%d" % n} for n in range(nodes)],
    }
    digest = route_hash(record)
    record["route_hash"], record["route_id"] = digest, route_id_from_hash(digest)
    return record


def write_json(path: Path, value, when: float = CLOCK_BASE - 600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    touch(path, when)


def attempt_row(stamp, status: str, slug: str, meta: dict, cwd: str) -> str:
    pairs = ",".join("%s=%s" % (key, value) for key, value in meta.items())
    # ISO stamps, as the real writers produce: the displayed "running Xm" derives from them.
    return "\t".join((stamp if isinstance(stamp, str) else iso(stamp),
                      status, cwd, cwd, slug, pairs))


class Fixture:
    """A small deterministic state tree.  Everything under ``root`` is ours."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.jobs = self.root / "registry" / ".dispatch" / "jobs.log"
        self.routes = self.root / "routes"
        self.claude = self.root / "claude-config"
        self.codex = self.root / "codex-home"
        self.work = self.root / "work"
        self.logs = self.jobs.parent / "logs"      # the registry's own log directory
        self.liveness = self.root / "liveness.json"
        # The seat ledgers (lineage) of every computing process: warm and fresh must read the same
        # ones, so each process's own ``session-tidy`` state is a link to this directory.
        self.seat_state = self.root / "seat-state"
        self.seat_tidy = self.seat_state / "hearting" / "session-tidy"
        self.revision = {0: 0, 1: 0, 2: 0}

    # -- paths ------------------------------------------------------------
    def route_path(self, index: int) -> Path:
        return self.routes / ("route-%d.json" % index)

    def outcome_path(self, index: int) -> Path:
        return self.routes / ("route-%d.outcome.json" % index)

    def node_route_path(self, index: int) -> Path:
        return self.routes / ("route-%d.node.json" % index)

    def claude_stream(self) -> Path:
        from claude_project_dir import encode_project_dir
        return (self.claude / "projects" / encode_project_dir(str(self.work))
                / (CLAUDE_SID + ".jsonl"))

    def codex_rollout(self, sid: str = CODEX_SID) -> Path:
        return (self.codex / "sessions" / "2027" / "01" / "15"
                / ("rollout-2027-01-15T07-50-00-%s.jsonl" % sid))

    def chain_route_path(self, index: int) -> Path:
        return self.routes / ("route-%d.chain.json" % index)

    def chain_outcome_path(self, index: int) -> Path:
        return self.routes / ("route-%d.chain.outcome.json" % index)

    def chain_dir(self) -> Path:
        return self.root / "route-chains"

    def write_chain_ledger(self) -> None:
        """The session-anchored route chain the Fleet displays for the claude session."""
        from fleet import route_chain
        lines = []
        for index in range(3):
            record = route_record(self.root, index, revision=self.revision[index])
            line = route_chain.build_line(
                {**record, "campaign_key": "camp-1", "slug": "slug-%d" % index},
                event="compile", harness="claude", session_id=CLAUDE_SID,
                route_file=self.chain_route_path(index), now=CLOCK_BASE - 4000 + index)
            lines.append(line)
        path = self.chain_dir() / "claude" / (CLAUDE_SID + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(line, sort_keys=True, ensure_ascii=False) + "\n"
                                for line in lines), encoding="utf-8")
        touch(path, CLOCK_BASE - 300)

    def worker_log(self, attempt: str = "att-child-0") -> Path:
        return self.logs / ("run.%s.claude.jsonl" % attempt)

    def quota_log(self, attempt: str) -> Path:
        return self.logs / ("run.%s.claude.jsonl" % attempt)

    # -- rows -------------------------------------------------------------
    def owner_and_child_rows(self) -> list[str]:
        out, cwd = [], str(self.work)
        for index in range(3):
            record = route_record(self.root, index, revision=self.revision[index])
            route_file = self.route_path(index)
            base = {
                "attempt_schema_version": "2", "capability": "autopilot-code",
                "parent_sid": CLAUDE_SID, "parent_harness": "claude",
                "route_id": record["route_id"], "route_hash": record["route_hash"],
                "route_file": str(route_file), "owner_route_id": record["route_id"],
                "owner_route_hash": record["route_hash"], "owner_route_file": str(route_file),
            }
            out.append(attempt_row(CLOCK_BASE - 3000 + index, "open", "slug-%d" % index, {
                **base, "harness": "claude", "dispatch_depth": "1", "worker_type": "owner",
                "attempt_id": "att-owner-%d" % index, "launch_claimed": "1",
                "launch_started": "1", "pid": str(CLAUDE_PID), "pid_start": "101"}, cwd))
            child = {**base, "dispatch_depth": "2", "worker_type": "child",
                     "attempt_id": "att-child-%d" % index, "route_node": "n0",
                     "parent_attempt_id": "att-owner-%d" % index,
                     "route_file": str(self.node_route_path(index))}
            if index == 0:
                child.update(harness="claude", transport="headless",
                             log_file=str(self.worker_log("att-child-0")),
                             pid=str(CODEX_PID), pid_start="102")
            else:
                child.update(harness="codex")
            out.append(attempt_row(CLOCK_BASE - 2900 + index, "open", "slug-%d" % index,
                                   child, cwd))
        return out

    def history_rows(self) -> list[str]:
        out, cwd = [], str(self.work)
        for index in range(8):
            out.append(attempt_row(CLOCK_BASE - 90_000 + index, "done", "old-%d" % index, {
                "attempt_schema_version": "2", "dispatch_depth": "2", "worker_type": "child",
                "harness": "codex", "attempt_id": "att-old-%d" % index,
                "parent_sid": OTHER_SID, "parent_harness": "claude",
                "ended_at": "%.3f" % (CLOCK_BASE - 80_000 + index),
                "note": "fixture-history", "padding": "p" * 120}, cwd))
        out.append(attempt_row(CLOCK_BASE - 5000, "open", "dup", {
            "attempt_schema_version": "2", "dispatch_depth": "2", "worker_type": "child",
            "harness": "codex", "attempt_id": "att-dup", "parent_sid": OTHER_SID,
            "parent_harness": "claude"}, cwd))
        out.append(attempt_row(CLOCK_BASE - 4900, "done", "dup", {
            "attempt_schema_version": "2", "dispatch_depth": "2", "worker_type": "child",
            "harness": "codex", "attempt_id": "att-dup", "parent_sid": OTHER_SID,
            "parent_harness": "claude", "ended_at": "%.3f" % (CLOCK_BASE - 4800)}, cwd))
        out.append(self.quota_row("att-quota-0"))
        return out

    def quota_row(self, attempt: str, *, observed: float = CLOCK_BASE - 7200) -> str:
        """A dead claude attempt whose own log carries a rejected native window."""
        return attempt_row(iso(observed), "done", "quota", {
            "attempt_schema_version": "2", "dispatch_depth": "2", "worker_type": "child",
            "harness": "claude", "attempt_id": attempt, "parent_sid": OTHER_SID,
            "parent_harness": "claude", "note": "dead-limit", "failure_class": "blocked",
            "log_file": str(self.quota_log(attempt)), "model": "fixture-model",
            "quota_scope": "scope-a", "quota_scope_kind": "login",
            "ended_at": "%.3f" % observed}, str(self.work))

    def write_quota_log(self, attempt: str, *, observed: float = CLOCK_BASE - 7200) -> None:
        sid = "quota-sid-" + attempt
        rows = [
            {"type": "rate_limit_event", "session_id": sid, "rate_limit_info": {
                "status": "rejected", "rateLimitType": "five_hour",
                "resetsAt": int(observed + 5 * 3600), "isUsingOverage": False}},
            {"type": "result", "session_id": sid, "is_error": True, "api_error_status": 429}]
        path = self.quota_log(attempt)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows),
                        encoding="utf-8")
        touch(path, observed)

    def rows(self) -> list[str]:
        return self.owner_and_child_rows() + self.history_rows()

    # -- streams ----------------------------------------------------------
    def claude_rows(self, extra: int = 0) -> list[dict]:
        rows = [{"type": "user", "sessionId": CLAUDE_SID, "timestamp": iso(CLOCK_BASE - 900),
                 "cwd": str(self.work), "message": {"role": "user", "content": "start"}},
                {"type": "assistant", "sessionId": CLAUDE_SID, "timestamp": iso(CLOCK_BASE - 800),
                 "message": {"role": "assistant", "content": [
                     {"type": "tool_use", "id": "toolu_a", "name": "Agent",
                      "input": {"subagent_type": "reviewer"}}]}}]
        for n in range(extra):
            rows.append({"type": "assistant", "sessionId": CLAUDE_SID,
                         "timestamp": iso(CLOCK_BASE - 700 + n),
                         "message": {"role": "assistant", "content": [
                             {"type": "tool_use", "id": "toolu_x%d" % n, "name": "Agent",
                              "input": {"subagent_type": "worker-%d" % n}}]}})
        return rows

    def codex_rows(self, turns: int = 1, *, complete_last: bool = False) -> list[dict]:
        rows = [{"type": "session_meta", "payload": {
            "id": CODEX_SID, "cwd": str(self.work), "timestamp": iso(CLOCK_BASE - 1000)}}]
        for turn in range(turns):
            rows.append({"type": "event_msg", "timestamp": iso(CLOCK_BASE - 900 + turn * 10),
                         "payload": {"type": "task_started", "turn_id": "turn-%d" % turn}})
            if turn < turns - 1 or complete_last:
                rows.append({"type": "event_msg", "timestamp": iso(CLOCK_BASE - 895 + turn * 10),
                             "payload": {"type": "task_complete", "turn_id": "turn-%d" % turn}})
        return rows

    def worker_rows(self, *, open_tool: str | None = None, extra: int = 0) -> list[dict]:
        sid = "worker-stream-sid"
        rows = [{"type": "system", "subtype": "init", "session_id": sid, "model": "fixture-model"},
                {"type": "assistant", "session_id": sid, "message": {
                    "role": "assistant", "model": "fixture-model", "content": [
                        {"type": "text", "text": "working"}],
                    "usage": {"input_tokens": 10, "output_tokens": 5}}}]
        for n in range(extra):
            rows.append({"type": "assistant", "session_id": sid, "message": {
                "role": "assistant", "model": "fixture-model", "content": [
                    {"type": "tool_use", "id": "toolu_w%d" % n, "name": "Read",
                     "input": {"file_path": "/x/%d" % n}}]}})
            rows.append({"type": "user", "session_id": sid, "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_w%d" % n, "content": "ok"}]}})
        if open_tool:
            rows.append({"type": "assistant", "session_id": sid, "message": {
                "role": "assistant", "model": "fixture-model", "content": [
                    {"type": "tool_use", "id": "toolu_open", "name": "Bash",
                     "input": {"command": open_tool}}]}})
        return rows

    @staticmethod
    def jsonl(rows: list[dict]) -> str:
        return "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)

    # -- liveness ---------------------------------------------------------
    @staticmethod
    def default_liveness() -> dict:
        return {"sessions": [
            {"harness": "claude", "pid": CLAUDE_PID, "proc_start": "101", "elapsed_min": 60,
             "cwd": "$WORK"},
            {"harness": "codex", "pid": CODEX_PID, "proc_start": "102", "elapsed_min": 17,
             "cwd": "$WORK"}], "alive": True}

    def write_liveness(self, value: dict, when: float = CLOCK_BASE - 600) -> None:
        write_json(self.liveness, value, when)

    def read_liveness(self) -> dict:
        return json.loads(self.liveness.read_text(encoding="utf-8"))

    # -- build ------------------------------------------------------------
    def build(self) -> "Fixture":
        for directory in (self.work, self.routes, self.claude / "sessions", self.jobs.parent,
                          self.codex / "sessions", self.logs, self.root / "artifacts", self.seat_tidy):
            directory.mkdir(parents=True, exist_ok=True)
        for index in range(3):
            record = route_record(self.root, index, revision=self.revision[index])
            write_json(self.route_path(index), record)
            write_json(self.node_route_path(index), record)
            write_json(self.chain_route_path(index), record)
            for sidecar in (self.outcome_path(index), self.chain_outcome_path(index)):
                write_json(sidecar, {"route_id": record["route_id"],
                                     "route_hash": record["route_hash"]})
        self.write_chain_ledger()
        self.write_registry()
        self.write_quota_log("att-quota-0")
        self.write_stream(self.claude_stream(), self.claude_rows(), CLOCK_BASE - 60)
        self.write_stream(self.codex_rollout(), self.codex_rows(), CLOCK_BASE - 50)
        self.write_stream(self.worker_log(), self.worker_rows(open_tool="sleep 1"),
                          CLOCK_BASE - 20)
        self.write_liveness(self.default_liveness())
        write_json(self.claude / "sessions" / ("%d.json" % CLAUDE_PID), {
            "pid": CLAUDE_PID, "sessionId": CLAUDE_SID, "cwd": str(self.work),
            "startedAt": int((CLOCK_BASE - 3600) * 1000), "procStart": "101",
            "status": "busy", "updatedAt": int((CLOCK_BASE - 30) * 1000),
            "statusUpdatedAt": int((CLOCK_BASE - 30) * 1000), "kind": "interactive"})
        return self

    def write_stream(self, path: Path, rows: list[dict], when: float) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.jsonl(rows), encoding="utf-8")
        touch(path, when)

    def write_registry(self, rows: list[str] | None = None) -> None:
        rows = self.rows() if rows is None else rows
        self.jobs.parent.mkdir(parents=True, exist_ok=True)
        self.jobs.write_text("\n".join(rows) + "\n", encoding="utf-8")
        touch(self.jobs, CLOCK_BASE - 120)

    # -- real writers -----------------------------------------------------
    def append_rows(self, rows: list[str]) -> None:
        import dispatch_contract_registry as registry
        registry._append_registry_rows(self.jobs, rows)

    def replace_registry(self, rows: list[str]) -> None:
        import dispatch_contract_registry as registry
        registry._atomic_registry_replace(self.jobs, rows)

    def current_rows(self) -> list[str]:
        return self.jobs.read_text(encoding="utf-8").splitlines()

    def lineage_file(self) -> Path:
        import dispatch_registry_cache as cache
        return cache.lineage_path(self.jobs)


DEMAND_KEY = "claude\0" + CLAUDE_SID
SECOND_DEMAND_KEY = "codex\0" + CODEX_SID


def demand_value(*keys: str) -> dict:
    return {key: CLOCK_BASE - 5 for key in keys}


# ---------------------------------------------------------------------------
# the one computation both paths run
# ---------------------------------------------------------------------------

HERMETIC_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "TZ")


def _expand(value, work: str):
    if isinstance(value, str):
        return value.replace("$WORK", work)
    if isinstance(value, list):
        return [_expand(item, work) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item, work) for key, item in value.items()}
    return value


@contextlib.contextmanager
def hermetic(fixture_root, scratch, clock: FixtureClock, cache_home=None):
    """Fixture-only environment, and the patches that turn live inputs into fixture data.

    The process census and liveness verdict come from ``liveness.json`` on every
    call, so a change there is an input like any other.  No cache is touched.
    ``scratch`` holds HOME/XDG state of one computing process; ``cache_home`` names
    another process's XDG cache (a RESUME computation reads what WARM left there).
    """
    scratch = Path(scratch)
    root = Path(fixture_root)
    fx = Fixture(root)
    state_home = scratch / "state"
    for directory in ("home", "state", "data", "cache", "tmp"):
        (scratch / directory).mkdir(parents=True, exist_ok=True)
    seat_link = state_home / "hearting" / "session-tidy"
    seat_link.parent.mkdir(parents=True, exist_ok=True)
    if not seat_link.is_symlink() and not seat_link.exists():
        seat_link.symlink_to(fx.seat_tidy)

    def census():
        return _expand(json.loads(fx.liveness.read_text(encoding="utf-8")), str(fx.work))

    def proc_start(pid):
        for item in census()["sessions"]:
            if item["pid"] == int(pid):
                return item.get("proc_start")
        return None

    def proc_rollout(pid, _cwd, _home):
        return str(fx.codex_rollout()) if int(pid) == CODEX_PID else None

    env = {
        "HOME": str(scratch / "home"), "XDG_STATE_HOME": str(state_home),
        "XDG_DATA_HOME": str(scratch / "data"),
        "XDG_CACHE_HOME": str(cache_home if cache_home is not None else scratch / "cache"),
        "TMPDIR": str(scratch / "tmp"), "AGENT_DISPATCH_JOBS": str(fx.jobs),
        "AGENT_RESOURCE_RUN_INDEX": str(scratch / "resource-runs.index.json"),
        "AGENT_HOME": str(ROOT), "CLAUDE_CONFIG_DIR": str(fx.claude),
        "HARNESS_STATE_ROOT": str(scratch / "harness-state"),
        "PYTHONDONTWRITEBYTECODE": "1", "FLEET_TITLE_DISABLE": "1",
        "FLEET_ROUTE_CHAIN_DIR": str(fx.chain_dir()),
    }
    for key in HERMETIC_ENV_KEYS:
        if key in os.environ and key != "PATH":
            env[key] = os.environ[key]
    # PATH holds only what the computation may run.  No herdr, tmux, ps or harness
    # binary is reachable, so no live pane census or process table leaks in.
    bin_dir = scratch / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, target in (("python3", sys.executable), ("sh", shutil.which("sh")),
                         ("bash", shutil.which("bash")), ("git", shutil.which("git"))):
        link = bin_dir / name
        if target and not link.exists():
            link.symlink_to(target)
    env["PATH"] = str(bin_dir)
    # Import everything the clock and the patches name *before* installing them,
    # so a module first imported inside the patch does not bind a real clock.
    import importlib
    for pkg in FLEET_PACKAGES:
        for name in ("procscan", "codex", "claude", "opencode", "dispatch", "liveness",
                     "usage_api", "memory", "shell_jobs"):
            importlib.import_module("%s.collectors.%s" % (pkg, name))
        importlib.import_module(pkg + ".titles")
    for name in ("route_authority", "human_wait_state", "dispatch_capacity_evidence",
                 "dispatch_contract_registry", "dispatch_registry_cache"):
        importlib.import_module(name)
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
        stack.enter_context(clock.installed())
        # Display-side requests and the detached producer are the driver's job
        # here (``place_demand``); a computation must not spawn or re-mark anything.
        producer = sys.modules["human_wait_state"]
        stack.enter_context(mock.patch.object(producer, "_ensure_producer", return_value=True))
        stack.enter_context(mock.patch.object(producer, "_request_producer", return_value=None))
        # The descendant-tag walk reads every process on the machine; there is none here.
        for name in ("dispatch_contract_process", "dispatch_contract"):
            module = importlib.import_module(name)
            empty = module.ProcessTableScan({}, {}) if hasattr(module, "ProcessTableScan") else None
            if hasattr(module, "scan_process_table") and empty is not None:
                stack.enter_context(mock.patch.object(
                    module, "scan_process_table", lambda empty=empty: empty))
        for pkg in FLEET_PACKAGES:
            procscan = sys.modules[pkg + ".collectors.procscan"]
            codex = sys.modules[pkg + ".collectors.codex"]
            dispatch = sys.modules[pkg + ".collectors.dispatch"]
            liveness = sys.modules[pkg + ".collectors.liveness"]
            usage_api = sys.modules[pkg + ".collectors.usage_api"]
            titles = sys.modules[pkg + ".titles"]
            model = importlib.import_module(pkg + ".model")

            def fresh_sessions(model=model, **_kwargs):
                sessions = []
                for item in census()["sessions"]:
                    session = model.Session(harness=item["harness"], pid=item["pid"],
                                            cwd=item["cwd"], elapsed_min=item["elapsed_min"])
                    session.proc_start = item.get("proc_start")
                    sessions.append(session)
                return sessions

            patch = lambda target, name, **kw: stack.enter_context(  # noqa: E731
                mock.patch.object(target, name, **kw))
            patch(procscan, "scan", side_effect=fresh_sessions)
            patch(procscan, "read_proc_start", side_effect=proc_start)
            patch(dispatch, "_scan_processes", return_value=[])
            patch(dispatch, "_candidate_jobs_paths", return_value=[str(fx.jobs)])
            patch(dispatch, "_pid_namespace_identity", return_value="pid:[fixture]")
            for module in (dispatch, codex, sys.modules[pkg + ".collectors.opencode"]):
                patch(module, "os", new=_ProcOs(census))
            patch(dispatch, "_codex_home", return_value=str(fx.codex))
            patch(dispatch, "_registry_home", return_value=str(root / "registry"))
            patch(codex, "_proc_rollout", side_effect=proc_rollout)
            patch(codex, "_home", return_value=str(fx.codex))
            patch(codex, "_config_model_effort", return_value=("gpt-fixture", "medium"))
            patch(codex, "account_usage", return_value=None)
            patch(usage_api, "account_usage", return_value=None)
            patch(liveness, "_alive", side_effect=lambda *a, **k: census().get("alive", True))
            patch(titles, "fresh_title", return_value=None)
            patch(titles, "fresh_summary", return_value=None)
        yield fx


class _ProcOs:
    """``os`` as one collector module sees it, except that ``/proc/<pid>`` of a census
    pid exists exactly while the census says the processes are alive."""

    def __init__(self, census):
        self._census = census
        outer = self

        class PathProxy:
            def __getattr__(self, name):
                return getattr(os.path, name)

            def exists(self, path):
                text = os.fspath(path)
                match = re.fullmatch(r"/proc/(\d+)", text)
                if match:
                    known = {item["pid"] for item in outer._census()["sessions"]}
                    if int(match.group(1)) in known:
                        return bool(outer._census().get("alive", True))
                return os.path.exists(path)

        self.path = PathProxy()

    def listdir(self, path="."):
        # A walk of the real process table would read every other process on the
        # machine; the fixture's table is its census.
        if os.fspath(path).rstrip("/") == "/proc":
            if not self._census().get("alive", True):
                return []
            return [str(item["pid"]) for item in self._census()["sessions"]]
        return os.listdir(path)

    def __getattr__(self, name):
        return getattr(os, name)


def demand_path() -> Path:
    import human_wait_state as producer
    return producer.state_root() / ".demand"


def place_demand(fixture_root, scratch, value: dict, when: float) -> None:
    """The display side's request marker; an external write from the producer's view."""
    with hermetic(fixture_root, scratch, FixtureClock(when)):
        path = demand_path()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(value).encode())
        finally:
            os.close(fd)
        touch(path, when - 1)


def _codex_disk_resumes() -> int:
    module = sys.modules.get("fleet.collectors.codex")
    return int(getattr(module, "_LIFECYCLE_DISK_RESUMES", 0)) if module is not None else 0


REGISTRY_COUNTERS = ("cache_hits", "content_reads", "delta_reads", "append_reuses",
                     "full_metadata_parses", "invalidations", "prefix_reuses",
                     "prefix_segments_reused", "line_parse_reuses",
                     "segment_resyncs", "segment_rebuilds")
CODEX_COUNTERS = ("hits", "append_reads", "full_reads")
OWNER_INPUT_COUNTERS = ("owner_decisions", "owner_decision_skips")
FOLD_COUNTERS = ("segment_folds", "segment_fold_reuses")
HOLDER_COUNTERS = ("holder_resolves", "holder_memo_hits")
GATE_COUNTERS = ("generation_segment_builds", "generation_segment_reuses",
                 "route_wait_recomputes", "route_wait_reuses")


def counters() -> dict:
    """What the warm process's incremental paths have done so far (monotone totals).

    Registry reader counters come from ``metrics_snapshot()``; ``codex_*`` from the
    lifecycle cursor, summed over both import spellings of the collector."""
    import dispatch_registry_cache as registry_cache
    snapshot = registry_cache.metrics_snapshot()
    out = {name: int(snapshot[name]) for name in REGISTRY_COUNTERS}
    for name in CODEX_COUNTERS:
        out["codex_" + name] = 0
    for pkg in FLEET_PACKAGES:
        module = sys.modules.get(pkg + ".collectors.codex")
        stats = getattr(module, "_LIFECYCLE_STATS", None) or {}
        for name in CODEX_COUNTERS:
            out["codex_" + name] += int(stats.get(name, 0))
    owner_input = sys.modules.get("dispatch_owner_input")
    owner_stats = owner_input.metrics_snapshot() if hasattr(owner_input, "metrics_snapshot") else {}
    for name in OWNER_INPUT_COUNTERS:
        out[name] = int(owner_stats.get(name, 0))
    for name in FOLD_COUNTERS:
        out[name] = 0
    for pkg in FLEET_PACKAGES:
        module = sys.modules.get(pkg + ".collectors.dispatch")
        stats = module.registry_fold_metrics() if hasattr(module, "registry_fold_metrics") else {}
        for name in FOLD_COUNTERS:
            out[name] += int(stats.get(name, 0))
    for name in HOLDER_COUNTERS:
        out[name] = 0
    handover = sys.modules.get("dispatch_seat_handover")
    for name, value in (handover.holder_metrics() if hasattr(handover, "holder_metrics") else {}).items():
        out[name] = int(value)
    for name in GATE_COUNTERS:
        out[name] = 0
    for module_name in ("human_gate_observation", "utilities.human_gate_observation"):
        module = sys.modules.get(module_name)
        stats = module.metrics_snapshot() if hasattr(module, "metrics_snapshot") else {}
        for name in GATE_COUNTERS:
            out[name] += int(stats.get(name, 0))
    return out


class ClockedTime:
    """``time`` as the registry reader sees it: its 30 s observation gap follows the
    injected clock, so whether the warm side may take a retained-descriptor shortcut
    never depends on how slow this machine is (E12 moves the clock past the gap)."""

    def __init__(self, clock: FixtureClock):
        self._clock = clock

    def monotonic(self) -> float:
        return self._clock.now

    def __getattr__(self, name):
        return getattr(_time, name)


def compute(fixture_root, scratch, now: float, mode: str = "warm", demand: dict | None = None,
            cache_home=None) -> dict:
    """O1..O4 for the fixture as it is now: a pure function of fixture and clock.

    ``mode``: ``warm`` is the long-lived process (small registry segments);
    ``fresh`` starts from nothing; ``resume`` starts from nothing except the parse
    cache directory ``cache_home`` that the warm process wrote.
    """
    clock = FixtureClock(now)
    if mode != "warm" and demand is not None:
        place_demand(fixture_root, scratch, demand, now)
    with hermetic(fixture_root, scratch, clock, cache_home) as fx:
        import dispatch_capacity_evidence as capacity
        import dispatch_registry_cache as registry_cache
        # Only the warm side's disk cache of capacity evidence is kept.
        disk = capacity._DISK_CACHE
        capacity._DISK_CACHE = disk and mode == "warm"
        chunk = WARM_CHUNK_LINES if mode == "warm" else registry_cache._PARSE_CHUNK_LINES
        resumed_before = _codex_disk_resumes()
        try:
            with mock.patch.object(registry_cache, "_PARSE_CHUNK_LINES", chunk), \
                    mock.patch.object(registry_cache, "time", ClockedTime(clock)):
                observed = {
                    "o1_registry": observe_registry(fx, capacity, clock),
                    "o2_streams": observe_streams(fx),
                    "o3_fleet": observe_fleet(Path(scratch)),
                    "o4_producer": observe_producer(),
                    "o5_holders": observe_holders(fx),
                }
            observed["_meta"] = {"lifecycle_disk_resumes": _codex_disk_resumes() - resumed_before}
            return observed
        finally:
            capacity._DISK_CACHE = disk


def _plain(value):
    if hasattr(value, "_asdict"):
        value = value._asdict()
    if isinstance(value, (dict, types.MappingProxyType, collections.abc.Mapping)):
        # Whatever mapping type holds the metadata (a dict, a mappingproxy, the Fleet's
        # RegistryMetadata), its items are what is observed.
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((_plain(item) for item in value), key=_canonical)
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "to_dict"):
        return _plain(value.to_dict())
    if hasattr(value, "__slots__") and not hasattr(value, "__dict__"):
        return {name: _plain(getattr(value, name, None)) for name in value.__slots__
                if not name.startswith("_")}
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return {key: _plain(item) for key, item in vars(value).items() if not key.startswith("_")}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _answer(reader):
    """What a reader answered, or the failure it raised: both are observed values."""
    try:
        return {"value": reader()}
    except Exception as error:  # noqa: BLE001
        return {"error": [type(error).__name__, getattr(error, "errno", None),
                          [str(arg) for arg in error.args if not isinstance(arg, int)]]}


def observe_registry(fx: Fixture, capacity, clock) -> dict:
    import dispatch_registry_cache as cache
    import dispatch_replacement
    from fleet.collectors import dispatch as fleet_dispatch
    token = str(fx.root)

    def view_answer():
        view = cache.registry_view(fx.jobs)
        records = [{
            "order": record.order, "status": record.status,
            "sha256": hashlib.sha256(record.raw.replace(token, "$FX").encode()).hexdigest(),
            "metadata": record.completion_metadata()} for record in view.records]
        return {"complete": not view.completeness_reason, "reason": view.completeness_reason,
                "lines": view.line_count, "partial_tail": bool(view.has_partial_tail),
                "records": records, "duplicates": bool(view.has_duplicate_attempts)}

    def replacement_answer():
        found = dispatch_replacement.registry_rows(fx.jobs)
        return {key: {"fields": value[0], "meta": value[1]} for key, value in found.items()}

    return {
        "view": _answer(view_answer),
        "parsed_rows": _answer(lambda: _plain(fleet_dispatch.parsed_registry_rows(fx.jobs))),
        "replacement_rows": _answer(replacement_answer),
        "capacity": _answer(lambda: _plain(capacity.observations(fx.jobs, now=clock.now, env={}))),
    }


def observe_streams(fx: Fixture) -> dict:
    from fleet.collectors import claude, codex
    return {
        "claude_subagents": _plain(claude._tail_subagents(str(fx.claude_stream()))),
        "codex_lifecycle": _plain(codex._latest_task_lifecycle(str(fx.codex_rollout()))),
        "codex_lifecycle_exact": _plain(codex._latest_task_lifecycle(
            str(fx.codex_rollout()), max_scan=None)),
    }


def observe_fleet(scratch: Path) -> dict:
    """The Fleet snapshot, read against a state directory nobody publishes into.

    The producer's published cache is an output of O4 and an input of the Fleet's
    answer-wait observation; a warm process has it from its previous pass and a
    new one does not.  That dependency is the producer's, compared in O4, so O3
    gets an empty one of its own on both sides.
    """
    import fleet.fleet as fleet_main
    from fleet.collectors import collect_all
    isolated = scratch / "state-fleet"
    isolated.mkdir(exist_ok=True)
    # The same seat ledgers as every other observation of this process (one lineage location, as
    # in a real Fleet: two locations alternating would only show a cache built for one of them).
    seat_link = isolated / "hearting" / "session-tidy"
    seat_link.parent.mkdir(parents=True, exist_ok=True)
    if not seat_link.is_symlink() and not seat_link.exists():
        seat_link.symlink_to(os.readlink(scratch / "state" / "hearting" / "session-tidy"))
    out = {}
    with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(isolated)}):
        for label, fast in (("fast_first", True), ("full", False)):
            snapshot = collect_all(harness_filter=None, include_report_verification=False,
                                   fast_first=fast)
            sessions, jobs = snapshot
            text = fleet_main._snapshot_json(
                sessions, jobs, resource_jobs=list(snapshot.resources),
                resource_diagnostics=snapshot.resource_diagnostics,
                observations=snapshot.observations, show_all=True)
            out[label] = json.loads(text)
    return out


def observe_holders(fx: Fixture) -> dict:
    """Which owner rows the Fleet's route display selects for one session, by who holds their job.

    Exactly the selection ``_prime_route_display_rows`` makes (the session holds a job when the
    lineage says so), with no alias or family rule mixed in, so a change of holder shows here.
    """
    import dispatch_registry_cache as cache
    import dispatch_supervision
    from fleet.collectors import dispatch as fleet_dispatch
    scope = {"aliases": set(), "routes": set(), "rows": {}, "sources": {},
             "holders": {("claude", CLAUDE_SID)}}
    with cache.registry_read_scope(), dispatch_supervision.route_attention_read_scope():
        fleet_dispatch._prime_route_display_rows([str(fx.jobs)], scope)
    view, chosen = scope["sources"][os.path.abspath(str(fx.jobs))]
    return {"selected": [{"order": record.order, "attempt": record.completion_metadata().get("attempt_id")}
                         for _order, record in sorted(chosen.items())],
            "routes": sorted(scope["routes"])}


def observe_producer() -> dict:
    """One producer pass; the published cache is the output."""
    import human_wait_state as producer
    with mock.patch.object(producer, "_notify_result", lambda _result: None):
        ok = producer._refresh_cache()
    out = {"ok": bool(ok)}
    for label, path in (("projection", producer._cache_path()),
                        ("fleet", producer._fleet_cache_path())):
        try:
            out[label] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out[label] = None
    return out


def main_child(argv) -> int:
    fixture_root, scratch, now, demand = argv[0], argv[1], float(argv[2]), json.loads(argv[3])
    mode = argv[4] if len(argv) > 4 else "fresh"
    cache_home = argv[5] if len(argv) > 5 else None
    sys.stdout.write(_canonical(compute(fixture_root, scratch, now, mode, demand, cache_home)))
    return 0


# ---------------------------------------------------------------------------
# comparison
# ---------------------------------------------------------------------------

def normalize(value, tokens: tuple[tuple[str, str], ...]):
    """Fixture and scratch roots become tokens; nothing else is rewritten."""
    if isinstance(value, str):
        for real, token in tokens:
            value = value.replace(real, token)
        for pattern, replacement, _reason in RUN_METADATA_REWRITES:
            value = pattern.sub(replacement, value)
        return value
    if isinstance(value, list):
        return [normalize(item, tokens) for item in value]
    if isinstance(value, dict):
        return {normalize(key, tokens): normalize(item, tokens) for key, item in value.items()}
    return value


def pointer_token(key) -> str:
    """RFC 6901 escaping; the NUL inside a demand key is shown as ``\\0``."""
    return str(key).replace("~", "~0").replace("/", "~1").replace("\x00", "\\0")


def first_difference(left, right, pointer: str = ""):
    """``(pointer, left, right)`` of the first unallowed difference, else None."""
    if any(pattern.search(pointer) for pattern, _reason in ALLOWED):
        return None
    if type(left) is not type(right):
        return pointer or "/", left, right
    if isinstance(left, dict):
        for key in sorted(set(left) | set(right), key=str):
            if key not in left or key not in right:
                found = (pointer + "/" + pointer_token(key),
                         left.get(key, "<absent>"), right.get(key, "<absent>"))
                if not any(p.search(found[0]) for p, _r in ALLOWED):
                    return found
                continue
            found = first_difference(left[key], right[key], pointer + "/" + pointer_token(key))
            if found:
                return found
        return None
    if isinstance(left, list):
        for index in range(max(len(left), len(right))):
            where = pointer + "/%d" % index
            if index >= len(left) or index >= len(right):
                if not any(p.search(where) for p, _r in ALLOWED):
                    return (where, left[index] if index < len(left) else "<absent>",
                            right[index] if index < len(right) else "<absent>")
                continue
            found = first_difference(left[index], right[index], where)
            if found:
                return found
        return None
    return None if left == right else (pointer or "/", left, right)


def brief(value, limit: int = 160) -> str:
    text = _canonical(value) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[:limit] + "...(%d chars)" % len(text)


# ---------------------------------------------------------------------------
# the lab: one event at a time, warm and fresh side by side
# ---------------------------------------------------------------------------

class Lab:
    def __init__(self, base: Path):
        self.base = Path(base)
        self.fresh_runs = 0

    def child_env(self) -> dict:
        env = {key: os.environ[key] for key in HERMETIC_ENV_KEYS if key in os.environ}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    def fresh(self, fixture_root, now: float, demand: dict) -> dict:
        scratch = Path(tempfile.mkdtemp(prefix="fresh-", dir=str(self.base)))
        try:
            done = subprocess.run(
                [sys.executable, str(SELF), "--compute", str(fixture_root), str(scratch),
                 repr(now), json.dumps(demand)],
                capture_output=True, text=True, env=self.child_env(), timeout=300, cwd=str(self.base))
            if done.returncode != 0:
                raise AssertionError("fresh child failed (%d): %s" % (done.returncode, done.stderr[-1500:]))
            self.fresh_runs += 1
            return normalize(json.loads(done.stdout), ((str(fixture_root), "$FX"), (str(scratch), "$SCRATCH")))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


    def resume(self, fixture_root, now: float, demand: dict, cache_home) -> dict:
        """A new process that starts from the parse cache the warm process wrote."""
        scratch = Path(tempfile.mkdtemp(prefix="resume-", dir=str(self.base)))
        try:
            done = subprocess.run(
                [sys.executable, str(SELF), "--compute", str(fixture_root), str(scratch),
                 repr(now), json.dumps(demand), "resume", str(cache_home)],
                capture_output=True, text=True, env=self.child_env(), timeout=300, cwd=str(self.base))
            if done.returncode != 0:
                raise AssertionError("resume child failed (%d): %s" % (done.returncode, done.stderr[-1500:]))
            return normalize(json.loads(done.stdout), ((str(fixture_root), "$FX"), (str(scratch), "$SCRATCH")))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


class Run:
    """The state of one event: its fixture, its clock, its warm process scratch."""

    def __init__(self, lab: Lab, eid: str):
        self.lab, self.eid = lab, eid
        self.dir = lab.base / eid
        self.dir.mkdir()
        self.fx = Fixture(self.dir / "fx").build()
        self.warm_scratch = self.dir / "warm"
        self.clock = FixtureClock(CLOCK_BASE)
        self.demand = demand_value(DEMAND_KEY)
        self.history: list[tuple[str, dict]] = []

    @property
    def tokens(self):
        return ((str(self.fx.root), "$FX"), (str(self.warm_scratch), "$SCRATCH"))

    def put_demand(self) -> None:
        place_demand(self.fx.root, self.warm_scratch, self.demand, self.clock.now)

    def warm(self) -> dict:
        return normalize(compute(self.fx.root, self.warm_scratch, self.clock.now, "warm"),
                         self.tokens)

    def fresh(self) -> dict:
        return self.lab.fresh(self.fx.root, self.clock.now, self.demand)

    def resume(self) -> dict:
        return self.lab.resume(self.fx.root, self.clock.now, self.demand,
                               self.warm_scratch / "cache")

    def mismatch(self, step: str, warm: dict, fresh: dict) -> str | None:
        found = first_difference(warm, fresh)
        if found is None:
            return None
        pointer, left, right = found
        return "%s %s: warm and fresh differ after step '%s' (warm=%s fresh=%s)" % (
            self.eid, pointer, step, brief(left), brief(right))


def changed(before: dict, after: dict) -> bool:
    return first_difference(before, after) is not None


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------
# An event is a list of steps.  Each step mutates the fixture (through the real
# writer wherever one exists), after which warm and fresh must agree, the fresh
# answer must have moved (self-check B), and one unchanged pass must still agree.

class Step:
    """One mutation. ``resume`` (E24..E27): also start a new process that finds the
    Codex cursor the warm process left on disk; ``"used"`` means it must have
    continued from it, ``"rejected"`` that it must have read the file from the start.
    ``expect``: which counters of the warm process must grow (``"+"``), stay
    still (``"0"``) or move by an exact amount (an int) during the step's warm pass, so an event cannot pass without
    taking the incremental path it is about; see ``counters`` and ``UNCHANGED_PASS``."""

    def __init__(self, label, mutate, *, clock_only=False, resume=None, expect=None, unchanged=False):
        self.label, self.mutate, self.clock_only = label, mutate, clock_only
        self.resume, self.expect = resume, dict(expect or {})
        # ``unchanged``: the step is an input that, by design, must not move any observed value
        # (the opposite of self-check B): the fresh output after it must equal the one before.
        self.unchanged = unchanged


# An unchanged pass after any step: every reader answers from what it kept.
UNCHANGED_PASS = {
    "holder_resolves": "0", "full_metadata_parses": "0", "prefix_reuses": "0", "append_reuses": "0",
    "line_parse_reuses": "0", "invalidations": "0", "content_reads": "0",
    "codex_append_reads": "0", "codex_full_reads": "0", "codex_hits": "+",
}

# What the registry reader must have done for the registry event of a step.  The
# fixture's warm side cuts the registry into 3-row segments, so "leading rows kept"
# is observable as reused segments.
REG_STILL = {"full_metadata_parses": "0", "prefix_reuses": "0", "append_reuses": "0",
             "invalidations": "0", "content_reads": "0", "cache_hits": "+"}
REG_APPEND = {"append_reuses": "+", "full_metadata_parses": "0", "prefix_reuses": "0"}
# Leading bytes identical: their segments are proven by digest and kept.  An append
# is a different path, so ``append_reuses`` must not be what moved.
REG_KEEP_PREFIX = {"prefix_reuses": "+", "append_reuses": "0", "full_metadata_parses": "0"}
# Nothing at the front survives (or the cache was dropped): every row is parsed again.
REG_REPARSE = {"full_metadata_parses": "+", "prefix_reuses": "0", "append_reuses": "0"}
CODEX_STILL = {"codex_append_reads": "0", "codex_full_reads": "0", "codex_hits": "+"}
CODEX_APPEND = {"codex_append_reads": "+", "codex_full_reads": "0"}
CODEX_REREAD = {"codex_full_reads": "+", "codex_append_reads": "0"}


def _new_open_row(fx: Fixture, name: str, **extra) -> str:
    return attempt_row(CLOCK_BASE - 100, "open", name, {
        "attempt_schema_version": "2", "dispatch_depth": "2", "worker_type": "child",
        "harness": "codex", "attempt_id": "att-" + name, "parent_sid": OTHER_SID,
        "parent_harness": "claude", **extra}, str(fx.work))


def e01(run):  # registry append: a new attempt (a dead quota attempt with its own log)
    run.fx.write_quota_log("att-quota-1", observed=CLOCK_BASE - 3600)
    run.fx.append_rows([run.fx.quota_row("att-quota-1", observed=CLOCK_BASE - 3600),
                        _new_open_row(run.fx, "fresh-one")])


def e02(run):  # the same attempt appended several times, one writer call each
    for status in ("running", "running", "done"):
        run.fx.append_rows([attempt_row(run.clock.now - 10, status, "att-child-2", {
            "attempt_schema_version": "2", "dispatch_depth": "2", "worker_type": "child",
            "harness": "codex", "attempt_id": "att-child-2", "parent_sid": CLAUDE_SID,
            "parent_harness": "claude",
            **({"ended_at": "%.3f" % (run.clock.now - 5)} if status == "done" else {})},
            str(run.fx.work))])


def _replaced_rows(run) -> list[str]:
    rows = run.fx.current_rows()
    rows[3] = rows[3].replace("\topen\t", "\tdone\t", 1)       # an early row changes
    rows.append(_new_open_row(run.fx, "after-replace"))
    return rows


def e03(run):  # whole-file replacement through the real writer, lineage recorded
    run.fx.replace_registry(_replaced_rows(run))


def e04(run):  # replacement, lineage sidecar gone
    run.fx.replace_registry(_replaced_rows(run))
    run.fx.lineage_file().unlink()


def e05(run):  # replacement, lineage that lies about the preserved prefix
    run.fx.replace_registry(_replaced_rows(run))
    path = run.fx.lineage_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    import dispatch_registry_cache as cache
    raw = run.fx.jobs.read_bytes()
    last = raw.rstrip(b"\n").rfind(b"\n") + 1               # claims all but the last row survived
    for entry in data["entries"]:
        entry["n"] = last
        entry["a"] = hashlib.sha256(raw[max(0, last - 4096):last]).hexdigest()
    path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    assert cache._read_lineage(run.fx.jobs) is not None, "E05 forged lineage was not even readable"


def e06a(run):  # an unfinished last line
    with run.fx.jobs.open("a", encoding="utf-8") as handle:
        handle.write(_new_open_row(run.fx, "torn")[:60])


def e06b(run):  # the same line completed
    with run.fx.jobs.open("a", encoding="utf-8") as handle:
        handle.write(_new_open_row(run.fx, "torn")[60:] + "\n")


def e07(run):  # content changes while size and mtime_ns are restored
    path = run.fx.jobs
    before = path.stat()
    raw = path.read_bytes()
    target = b"att-owner-1"
    assert target in raw
    changed_raw = raw.replace(target, b"att-owner-X", 1)
    assert len(changed_raw) == len(raw)
    path.write_bytes(changed_raw)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)


def e08(run):  # truncate: the file shrinks (last rows disappear)
    rows = run.fx.current_rows()[:-3]
    run.fx.jobs.write_text("\n".join(rows) + "\n", encoding="utf-8")


def e09(run):  # rotation: old file renamed away, a new smaller file takes the name
    rotated = run.fx.jobs.with_name("jobs.log.1")
    os.rename(run.fx.jobs, rotated)
    run.fx.write_registry(run.fx.owner_and_child_rows() + [
        _new_open_row(run.fx, "rotated-in")])


def e10a(run):  # deleted
    run.fx.jobs.unlink()


def e10b(run):  # recreated, different content
    run.fx.write_registry(run.fx.owner_and_child_rows() + [
        _new_open_row(run.fx, "recreated")])


def e12a(run):
    run.clock.advance(120)


def e12b(run):
    run.clock.advance(61)


def e13a(run):  # liveness only: the processes are no longer alive
    value = run.fx.read_liveness()
    value["alive"] = False
    run.fx.write_liveness(value, run.clock.now - 1)


def e13b(run):  # liveness only: the pid is reused, another start time
    value = run.fx.read_liveness()
    value["alive"] = True
    value["sessions"][0]["proc_start"] = "999"
    run.fx.write_liveness(value, run.clock.now - 1)


def e14(run):  # another set of sessions: the codex session is replaced, a new claude session id
    value = run.fx.read_liveness()
    value["sessions"][1] = {"harness": "codex", "pid": CODEX_PID + 5, "proc_start": "205",
                            "elapsed_min": 3, "cwd": "$WORK"}
    run.fx.write_liveness(value, run.clock.now - 1)
    write_json(run.fx.claude / "sessions" / ("%d.json" % CLAUDE_PID), {
        "pid": CLAUDE_PID, "sessionId": OTHER_SID, "cwd": str(run.fx.work),
        "startedAt": int((CLOCK_BASE - 100) * 1000), "procStart": "101", "status": "idle",
        "updatedAt": int(run.clock.now * 1000), "statusUpdatedAt": int(run.clock.now * 1000),
        "kind": "interactive"}, run.clock.now - 1)


def e15(run):  # a different generation of route 1: the file is rewritten, its hash changes
    run.fx.revision[1] = 1
    record = route_record(run.fx.root, 1, revision=1)
    write_json(run.fx.route_path(1), record, run.clock.now - 1)
    write_json(run.fx.node_route_path(1), record, run.clock.now - 1)


def e16a(run):  # a worker stream grows: it is now at another tool call
    run.fx.write_stream(run.fx.worker_log(), run.fx.worker_rows(open_tool="make test", extra=2),
                        run.clock.now - 1)


def e16b(run):  # the same stream grows again, the open call closes
    rows = run.fx.worker_rows(extra=3)
    run.fx.write_stream(run.fx.worker_log(), rows, run.clock.now - 1)


def e17a(run):  # the sidecar of a route the session chain names
    record = route_record(run.fx.root, 0)
    write_json(run.fx.chain_outcome_path(0), {
        "route_id": record["route_id"], "route_hash": record["route_hash"],
        "terminal_gate_proven": True, "closed_at": iso(run.clock.now - 30)}, run.clock.now - 1)


def e17b(run):  # the sidecar of a route a registry row names
    record = route_record(run.fx.root, 0)
    write_json(run.fx.outcome_path(0), {
        "route_id": record["route_id"], "route_hash": record["route_hash"],
        "terminal_gate_proven": True, "closed_at": iso(run.clock.now - 20)}, run.clock.now - 1)


def e18(run):  # only the file a session-chain node names changes (another round of that route)
    record = route_record(run.fx.root, 2, revision=3)
    write_json(run.fx.chain_route_path(2), record, run.clock.now - 1)


def e19(run):  # an external write lands in the directory the warm pass itself created
    run.demand = demand_value(DEMAND_KEY, SECOND_DEMAND_KEY)
    run.put_demand()


def e20a(run):  # codex lifecycle log appended: the running turn completes, a new turn starts
    rows = run.fx.codex_rows(turns=3, complete_last=False)
    run.fx.write_stream(run.fx.codex_rollout(), rows, run.clock.now - 1)


def e20b(run):  # truncated: the log shrinks back
    run.fx.write_stream(run.fx.codex_rollout(), run.fx.codex_rows(turns=1), run.clock.now - 1)


def e20c(run):  # rotated: renamed away, a new log with another history under the same name
    path = run.fx.codex_rollout()
    os.rename(path, path.with_name(path.name + ".old"))
    rows = run.fx.codex_rows(turns=2, complete_last=True)
    run.fx.write_stream(path, rows, run.clock.now - 1)


def e21(run):  # a replacement that keeps the leading rows: one middle row changes, one is added
    rows = run.fx.current_rows()
    rows[9] = rows[9].replace("fixture-history", "fixture-history-edited", 1)
    rows.append(_new_open_row(run.fx, "after-middle-edit"))
    run.fx.replace_registry(rows)


def e22(run):  # the first rows leave (an archive): every later row moves up
    run.fx.replace_registry(run.fx.current_rows()[2:] + [_new_open_row(run.fx, "after-archive")])


def e23(run):  # one middle row is deleted and a row is added: later rows keep their text, not their order
    rows = run.fx.current_rows()
    del rows[8]
    rows.append(_new_open_row(run.fx, "after-delete"))
    run.fx.replace_registry(rows)


def _append_rollout(run, rows: list[dict]) -> None:
    path = run.fx.codex_rollout()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(run.fx.jsonl(rows))
    touch(path, run.clock.now - 1)


def e24(run):  # codex lifecycle log appended after the cursor was left on disk
    _append_rollout(run, run.fx.codex_rows(turns=3, complete_last=False)[2:])


def _codex_filler(when: float) -> dict:
    return {"type": "response_item", "timestamp": iso(when), "payload": {
        "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "filler " * 20}]}}


def e25a(run):  # rotated to a log longer than the head digest (4 KiB) plus the boundary window
    path = run.fx.codex_rollout()
    os.rename(path, path.with_name(path.name + ".old"))
    rows = run.fx.codex_rows(turns=2)
    rows[1:1] = [_codex_filler(CLOCK_BASE - 950)] * 30
    run.fx.write_stream(path, rows, run.clock.now - 1)


def e25(run):  # bytes just before the saved offset change in place, past the head; the log grows
    path = run.fx.codex_rollout()
    raw = path.read_bytes()
    at = raw.rindex(b"turn-1")
    # Only the boundary digest can see this change; the head digest covers the first 4 KiB.
    assert len(raw) - at <= 256 and at >= 4096, (at, len(raw))
    path.write_bytes(raw[:at] + b"turn-X" + raw[at + len(b"turn-1"):])
    # A row that carries no lifecycle: a process that kept the stale cursor still says turn-1.
    _append_rollout(run, [_codex_filler(run.clock.now - 5)])


def e26(run):  # rotated: another file (new inode) under the same name, longer than the saved offset
    path = run.fx.codex_rollout()
    os.rename(path, path.with_name(path.name + ".old"))
    run.fx.write_stream(path, run.fx.codex_rows(turns=4, complete_last=True), run.clock.now - 1)


def e27(run):  # truncated in place and written again, longer than the saved offset, other turns
    rows = run.fx.codex_rows(turns=1)[:1]
    for turn in range(5):
        rows.append({"type": "event_msg", "timestamp": iso(CLOCK_BASE - 800 + turn),
                     "payload": {"type": "task_started", "turn_id": "other-%d" % turn}})
        rows.append({"type": "event_msg", "timestamp": iso(CLOCK_BASE - 790 + turn),
                     "payload": {"type": "task_complete", "turn_id": "other-%d" % turn}})
    run.fx.write_stream(run.fx.codex_rollout(), rows, run.clock.now - 1)


def e28a(run):  # a closed-route sidecar the registry row names (as E17b, other time)
    record = route_record(run.fx.root, 0)
    write_json(run.fx.outcome_path(0), {
        "route_id": record["route_id"], "route_hash": record["route_hash"],
        "terminal_gate_proven": True, "closed_at": iso(CLOCK_BASE - 2000)}, CLOCK_BASE - 1)


def e28b(run):  # the same sidecar, one digit of closed_at changed, size and mtime_ns restored
    path = run.fx.outcome_path(0)
    before = path.stat()
    raw = path.read_bytes()
    stamp = iso(CLOCK_BASE - 2000).encode()
    other = iso(CLOCK_BASE - 2000 + 600).encode()
    assert stamp in raw and len(stamp) == len(other) and stamp != other
    path.write_bytes(raw.replace(stamp, other, 1))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)


def e29(run):  # the claude transcript's agent type changes, size and mtime_ns restored
    path = run.fx.claude_stream()
    before = path.stat()
    raw = path.read_bytes()
    assert b"reviewer" in raw
    path.write_bytes(raw.replace(b"reviewer", b"reviewex", 1))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)


def _append_batches(run, names=("batch-a", "batch-b", "batch-c")) -> None:
    """Rows arrive by append, one batch each: the registry then holds segments of unequal sizes."""
    sizes = (2, 3, 1)
    for number, name in enumerate(names):
        run.fx.append_rows([_new_open_row(run.fx, "%s-%d" % (name, row)) for row in range(sizes[number])])


# ---------------------------------------------------------------------------
# BLOCKED owners (answer-wait S3): a route the compiler really emitted, so the owner-input
# decision runs ``verify_route`` to its end when it runs at all.
# ---------------------------------------------------------------------------

_REAL_ROUTE: list = []


def real_route_record(slug: str) -> dict:
    """The compiled route of this tree, ``slug`` changed and hash and id derived again."""
    if not _REAL_ROUTE:
        import hook_perf_fixture
        record, reason = hook_perf_fixture.compile_real_route(ROOT)
        assert record is not None, "no verifiable route for the BLOCKED owners: %s" % reason
        _REAL_ROUTE.append(record)
    from route_identity import route_hash, route_id_from_hash
    record = dict(_REAL_ROUTE[0])
    record["slug"], record["slug_truncated"] = slug, False
    digest = route_hash(record)
    record["route_hash"], record["route_id"] = digest, route_id_from_hash(digest)
    return record


def blocked_owner(run, name: str, ended: float | None = None) -> dict:
    """One owner that ended BLOCKED with no successor, on its own open route; returns its identity."""
    record = real_route_record("blocked-" + name)
    route_file = run.fx.routes / ("blocked-%s.json" % name)
    write_json(route_file, record, CLOCK_BASE - 3600)
    attempt = "att-blocked-" + name
    journal = run.fx.jobs.parent / "workflow" / record["route_id"] / "journal.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(json.dumps({"route_id": record["route_id"], "route_hash": record["route_hash"],
                                   "workflow_state": "RUNNING", "at": iso(CLOCK_BASE - 3600)}) + "\n",
                       encoding="utf-8")
    row = attempt_row(CLOCK_BASE - 3000, "done", "blocked-" + name, {
        "attempt_schema_version": "2", "capability": "autopilot-code", "harness": "claude",
        "dispatch_depth": "1", "worker_type": "owner", "attempt_id": attempt,
        "parent_sid": CLAUDE_SID, "parent_harness": "claude",
        "route_id": record["route_id"], "route_hash": record["route_hash"], "route_file": str(route_file),
        "owner_route_id": record["route_id"], "owner_route_hash": record["route_hash"],
        "owner_route_file": str(route_file), "launch_claimed": "1", "launch_started": "1",
        "note": "dead-worker-blocked",
        "ended_at": "%.3f" % (CLOCK_BASE - 2900 if ended is None else ended)}, str(run.fx.work))
    return {"attempt": attempt, "row": row, "route_id": record["route_id"], "journal": journal,
            "route_file": route_file}


def keep_answer(run, attempt: str, state: str = "retained") -> Path:
    """The owner's own ``.input.json`` with one request in ``state`` (an unconsumed one is a kept answer)."""
    from dispatch_contract import supervisor_lease_path
    path = supervisor_lease_path(run.fx.jobs, attempt).with_suffix(".input.json")
    write_json(path, {"schema_version": 1, "attempt_id": attempt, "requests": [
        {"id": "req-1", "digest": "sha256:fixture", "state": state, "text": "kept answer"}]},
        run.clock.now - 1)
    return path


def blocked_setup(run):  # two BLOCKED owners, neither holds an answer
    owners = [blocked_owner(run, name) for name in ("a", "b")]
    run.blocked = {owner["attempt"]: owner for owner in owners}
    run.fx.append_rows([owner["row"] for owner in owners])


def blocked_near_fold_setup(run):  # BLOCKED owners that end 100 s short of the 72 h fold window
    owners = [blocked_owner(run, name, ended=CLOCK_BASE - 72 * 3600 + 100) for name in ("a", "b")]
    run.blocked = {owner["attempt"]: owner for owner in owners}
    run.fx.append_rows([owner["row"] for owner in owners])


def e40a(run):  # an answer is kept for owner a: the owner-input decision runs for it (and only it)
    keep_answer(run, "att-blocked-a")


def e40b(run):  # the answer is consumed: nothing kept again
    keep_answer(run, "att-blocked-a", state="consumed")


def _unreadable_journal(run, attempt: str) -> None:
    journal = run.blocked[attempt]["journal"]
    journal.unlink()
    journal.mkdir()         # reading it raises IsADirectoryError, as an unreadable file would


def e40c(run):  # owner b holds no answer and its journal cannot be read: the decision is not asked
    _unreadable_journal(run, "att-blocked-b")


def e40d(run):  # owner a holds an answer and its journal cannot be read: the decision raises
    keep_answer(run, "att-blocked-a")
    _unreadable_journal(run, "att-blocked-a")


def route0_pairs(run, name: str) -> dict:
    """Metadata of a row on route 0's exact route triple (its owner's), as an owner of that route writes it."""
    record = route_record(run.fx.root, 0, revision=run.fx.revision[0])
    route_file = str(run.fx.route_path(0))
    return {"attempt_schema_version": "2", "capability": "autopilot-code", "harness": "claude",
            "dispatch_depth": "1", "worker_type": "owner", "attempt_id": "att-" + name,
            "parent_sid": CLAUDE_SID, "parent_harness": "claude",
            "route_id": record["route_id"], "route_hash": record["route_hash"], "route_file": route_file,
            "owner_route_id": record["route_id"], "owner_route_hash": record["route_hash"],
            "owner_route_file": route_file}


def e34a(run):  # an attempt row of route 0 is appended
    run.fx.append_rows([attempt_row(run.clock.now - 20, "running", "slug-0", {
        **route0_pairs(run, "route0-more"), "launch_claimed": "1"}, str(run.fx.work))])


def e34b(run):  # the same attempt is appended several times, one writer call each
    for status in ("running", "running", "done"):
        run.fx.append_rows([attempt_row(run.clock.now - 10, status, "slug-0", {
            **route0_pairs(run, "route0-again"),
            **({"ended_at": "%.3f" % (run.clock.now - 5)} if status == "done" else {})}, str(run.fx.work))])


def e34c(run):  # in place: an early history row and a BLOCKED owner row change; the segments between keep theirs
    rows = run.fx.current_rows()
    rows[9] = rows[9].replace("fixture-history", "fixture-history-edited-early", 1)
    at = next(index for index, row in enumerate(rows) if "att-blocked-a" in row)
    rows[at] = rows[at].replace("ended_at=%.3f" % (CLOCK_BASE - 2900),
                                "ended_at=%.3f" % (CLOCK_BASE - 2800), 1)
    assert rows[at] != run.fx.current_rows()[at] and rows[9] != run.fx.current_rows()[9]
    run.fx.replace_registry(rows)


def e34d(run):  # an owner that began later on the same route: the BLOCKED owner is no longer the last
    owner = run.blocked["att-blocked-a"]
    record = json.loads(owner["route_file"].read_text(encoding="utf-8"))
    run.fx.append_rows([attempt_row(run.clock.now - 15, "open", "blocked-a-next", {
        "attempt_schema_version": "2", "capability": "autopilot-code", "harness": "claude",
        "dispatch_depth": "1", "worker_type": "owner", "attempt_id": "att-blocked-a-next",
        "parent_sid": CLAUDE_SID, "parent_harness": "claude",
        "route_id": record["route_id"], "route_hash": record["route_hash"], "route_file": str(owner["route_file"]),
        "owner_route_id": record["route_id"], "owner_route_hash": record["route_hash"],
        "owner_route_file": str(owner["route_file"]), "launch_claimed": "1", "launch_started": "1"},
        str(run.fx.work))])


def _alias_row(run, name: str, stamp, route: dict) -> str:
    return attempt_row(stamp, "done", "alias-" + name, {
        "attempt_schema_version": "2", "dispatch_depth": "2", "worker_type": "child", "harness": "codex",
        "attempt_id": "att-alias-" + name, "parent_sid": OTHER_SID, "parent_harness": "claude",
        "route_id": route["route_id"], "route_hash": route["route_hash"], "route_file": route["route_file"]},
        str(run.fx.work))


def _alias_route(run) -> dict:
    record = route_record(run.fx.root, 2, revision=0)
    return {"route_id": record["route_id"], "route_hash": record["route_hash"],
            "route_file": str(run.fx.root / "routes" / "alias-route.json")}


def e34e1(run):  # the first row of a route has no usable time (its activity is nan)
    run.fx.append_rows([_alias_row(run, "x1", "not-a-time", _alias_route(run))])


def e34e2(run):  # a later row of the same route has one: the route's activity is now that time
    run.fx.append_rows([_alias_row(run, "x2", iso(CLOCK_BASE - 500), _alias_route(run))])


def e34e3(run):  # an even later row has none again: it does not take the time away
    run.fx.append_rows([_alias_row(run, "x3", "still-not-a-time", _alias_route(run))])


def raise_gate(run, index: int, gate: str = "approval", at: float | None = None) -> None:
    """The route's workflow journal gains a raised human gate, as the supervisor writes it."""
    record = route_record(run.fx.root, index, revision=run.fx.revision[index])
    journal = run.fx.jobs.parent / "workflow" / record["route_id"] / "journal.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "route_id": record["route_id"], "route_hash": record["route_hash"],
            "workflow_state": "BLOCKED_HUMAN_GATE", "at": iso(CLOCK_BASE - 900 if at is None else at),
            "evidence": {"gate": gate}}, sort_keys=True) + "\n")


def e32a(run):  # route 2's journal gains a raised gate: its wait appears
    raise_gate(run, 2)


def e33_gate(run):  # route 1 has a raised gate, so deleting its record visibly removes the wait
    raise_gate(run, 1)


def e33a(run):  # route 1's record file is replaced by identical content (a new inode)
    path = run.fx.route_path(1)
    body = path.read_bytes()
    tmp = path.with_name(path.name + ".new")
    tmp.write_bytes(body)
    os.replace(tmp, path)


def e33b(run):  # route 1's record file is deleted
    run.fx.route_path(1).unlink()


def e33c(run):  # and written again with the same content
    write_json(run.fx.route_path(1), route_record(run.fx.root, 1, revision=run.fx.revision[1]),
               run.clock.now - 1)


def e37a(run):  # the clock crosses the 72 h window a wait folds at; no input of any route changed
    run.clock.advance(150)


def e30(run):  # both files route 1 is read from change, size and mtime restored (new inodes)
    for path in (run.fx.route_path(1), run.fx.node_route_path(1)):
        before = path.stat()
        raw = path.read_bytes()
        marker = b'"fixture_revision": 0'
        assert marker in raw, "the route record no longer carries its revision marker"
        path.write_bytes(raw.replace(marker, b'"fixture_revision": 7', 1))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = path.stat()
        assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)


def _visibility_sidecar(run) -> Path:
    record = route_record(run.fx.root, 0)
    return run.fx.root / "artifacts" / ".runtime" / "routes" / (record["route_id"] + ".outcome.json")


def e31a(run):  # a closed-route sidecar where the route-visibility reader looks for it
    record = route_record(run.fx.root, 0)
    write_json(_visibility_sidecar(run), {
        "route_id": record["route_id"], "route_hash": record["route_hash"],
        "terminal_gate_proven": True, "closed_at": iso(CLOCK_BASE - 2000)}, CLOCK_BASE - 1)


def e31b(run):  # the same sidecar, one digit of closed_at changed, size and mtime_ns restored
    path = _visibility_sidecar(run)
    before = path.stat()
    raw = path.read_bytes()
    stamp = iso(CLOCK_BASE - 2000).encode()
    other = iso(CLOCK_BASE - 2000 + 600).encode()
    assert stamp in raw and len(stamp) == len(other) and stamp != other
    path.write_bytes(raw.replace(stamp, other, 1))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)


def e42_batches(run):
    _append_batches(run)


def e42a(run):  # a middle row is replaced in place by a longer one: later segments keep their bytes
    rows = run.fx.current_rows()
    rows[9] = rows[9].replace("fixture-history", "fixture-history-edited-in-place", 1)
    run.fx.replace_registry(rows)


def e42b(run):  # two rows, one in the middle and one near the end, change with their length kept
    rows = run.fx.current_rows()
    rows[7] = rows[7].replace("fixture-history", "fixture-histor!", 1)
    assert "batch-b-2" in rows[-2]
    rows[-2] = rows[-2].replace("batch-b-2", "batch-b-9", 1)
    run.fx.replace_registry(rows)


def e42c(run):  # an early row is deleted: every later row moves up, no old segment can be kept
    rows = run.fx.current_rows()
    del rows[1]
    run.fx.replace_registry(rows)


def e43_edit(run):  # an owner row of the claude session is edited in place (its own bytes change)
    rows = run.fx.current_rows()
    at = next(index for index, row in enumerate(rows) if "att-owner-1" in row)
    rows[at] = rows[at].replace("launch_started=1", "launch_started=1,fixture_note=edited", 1)
    assert rows[at] != run.fx.current_rows()[at]
    run.fx.replace_registry(rows)


def e43_handover(run):  # the claude session hands its unfinished jobs to a successor, by the real writer
    import dispatch_seat_handover as handover
    import session_tidy as tidy
    seat = tidy.Seat("pane", "fixture-seat", "fixture:pane")
    with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(run.fx.seat_state)}):
        row = handover.record_retire_handover(CLAUDE_SID, "claude", SUCCESSOR_SID, "claude",
                                              jobs=run.fx.jobs, now=run.clock.now - 5, successor_seat=seat)
    assert row is not None and row["bindings"], "the writer bound nothing: the event tested nothing"


def e38_lease(run):  # a BLOCKED owner's supervisor lease is taken and released (flock): no file content moves
    import fcntl
    from dispatch_contract import supervisor_lease_path
    path = supervisor_lease_path(run.fx.jobs, "att-blocked-a")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()


def e38_gone(run):  # the processes of the sessions end: liveness only, no registry or route file moves
    value = run.fx.read_liveness()
    value["alive"] = False
    run.fx.write_liveness(value, run.clock.now - 1)


def e39_other_session(run):  # the registry is replaced (new inode) with one more owner row, of another session
    rows = run.fx.current_rows()
    rows.append(attempt_row(CLOCK_BASE - 90, "open", "other-session-owner", {
        "attempt_schema_version": "2", "capability": "autopilot-code", "harness": "codex",
        "dispatch_depth": "1", "worker_type": "owner", "attempt_id": "att-owner-other",
        "parent_sid": OTHER_SID, "parent_harness": "claude", "launch_claimed": "1",
        "launch_started": "1"}, str(run.fx.work)))
    run.fx.replace_registry(rows)


def e39_same(run):  # the same rows, written again as another inode
    run.fx.replace_registry(run.fx.current_rows())


EVENTS = [
    ("E01", "registry append: a new attempt row",
     [Step("append", e01, expect={**REG_APPEND, **CODEX_STILL})]),
    ("E02", "the same attempt appended several times",
     [Step("append x3", e02, expect={**REG_APPEND, **CODEX_STILL})]),
    ("E03", "whole registry replaced by the real writer, lineage recorded",
     [Step("replace", e03, expect={**REG_KEEP_PREFIX, "line_parse_reuses": "+", **CODEX_STILL})]),
    ("E04", "whole registry replaced, lineage sidecar missing",
     [Step("replace", e04, expect={**REG_KEEP_PREFIX, "line_parse_reuses": "+", **CODEX_STILL})]),
    ("E05", "whole registry replaced, lineage lies about the preserved prefix",
     [Step("replace", e05, expect={**REG_KEEP_PREFIX, "line_parse_reuses": "+", **CODEX_STILL})]),
    ("E06", "an unfinished last line, completed on the next step",
     [Step("torn append", e06a, expect={**REG_APPEND, **CODEX_STILL}),
      Step("completed", e06b, expect={**REG_APPEND, **CODEX_STILL})]),
    # E07: the changed bytes sit in the first segment, so no segment may be kept.
    ("E07", "content changed with size and mtime_ns restored",
     [Step("same stat", e07, expect={**REG_REPARSE, **CODEX_STILL})]),
    # E08/E09: the leading rows are byte-identical, so a digest-proven prefix is kept;
    # what must not happen is that a shrink or a new file is treated as an append.
    ("E08", "truncate: the registry shrinks",
     [Step("truncate", e08, expect={**REG_KEEP_PREFIX, **CODEX_STILL})]),
    ("E09", "rotation: renamed away, a new file under the name",
     [Step("rotate", e09, expect={**REG_KEEP_PREFIX, **CODEX_STILL})]),
    ("E10", "deleted, then recreated",
     [Step("deleted", e10a, expect={"invalidations": "+", "append_reuses": "0",
                                    "prefix_reuses": "0", **CODEX_STILL}),
      Step("recreated", e10b, expect={**REG_REPARSE, **CODEX_STILL})]),
    ("E12", "the clock only moves (+120 s, +61 s)",
     [Step("+120 s", e12a, clock_only=True, expect={**REG_STILL, **CODEX_STILL}),
      Step("+61 s", e12b, clock_only=True, expect={**REG_STILL, **CODEX_STILL})]),
    ("E13", "liveness only: not alive, then another process start",
     [Step("not alive", e13a, expect={**REG_STILL, **CODEX_STILL}),
      Step("pid reused", e13b, expect={**REG_STILL, **CODEX_STILL})]),
    ("E14", "a different set of sessions",
     [Step("replace sessions", e14, expect={**REG_STILL, "codex_append_reads": "0",
                                            "codex_full_reads": "0"})]),
    ("E15", "another route generation: the route file is rewritten",
     [Step("rewrite route", e15, expect={**REG_STILL, **CODEX_STILL})]),
    ("E16", "a worker stream grows (an open tool call, then closed)",
     [Step("open tool", e16a, expect={**REG_STILL, **CODEX_STILL}),
      Step("closed", e16b, expect={**REG_STILL, **CODEX_STILL})]),
    ("E17", "only the route .outcome.json sidecar changes",
     [Step("chain sidecar", e17a, expect={**REG_STILL, **CODEX_STILL}),
      Step("registry sidecar", e17b, expect={**REG_STILL, **CODEX_STILL})]),
    ("E18", "only the file a session-chain node names changes",
     [Step("chain node file", e18, expect={**REG_STILL, **CODEX_STILL})]),
    ("E19", "external write into a directory the warm pass created",
     [Step("demand", e19, expect={**REG_STILL, **CODEX_STILL})]),
    ("E20", "codex lifecycle log: append, truncate, rotate",
     [Step("append", e20a, expect={**REG_STILL, **CODEX_APPEND}),
      Step("truncate", e20b, expect={**REG_STILL, **CODEX_REREAD}),
      Step("rotate", e20c, expect={**REG_STILL, **CODEX_REREAD})]),
    ("E21", "whole registry replaced, leading rows kept, a middle row changed",
     [Step("replace", e21, expect={**REG_KEEP_PREFIX, **CODEX_STILL})]),
    # E22: nothing at the front survives, but lines that only moved keep their parse.
    ("E22", "whole registry replaced, the first rows removed (order shifts)",
     [Step("replace", e22, expect={**REG_REPARSE, "line_parse_reuses": "+", **CODEX_STILL})]),
    ("E23", "whole registry replaced, a middle row deleted and a row added",
     [Step("replace", e23, expect={**REG_KEEP_PREFIX, "line_parse_reuses": "+", **CODEX_STILL})]),
    ("E24", "a new process continues the codex cursor from disk, the log grew",
     [Step("append after saved cursor", e24, resume="used",
           expect={**REG_STILL, **CODEX_APPEND})]),
    ("E25", "a new process finds the bytes before the saved offset changed",
     [Step("rotate to a log longer than the head digest", e25a,
           expect={**REG_STILL, **CODEX_REREAD}),
      Step("boundary rewritten, then appended", e25, resume="rejected",
           expect={**REG_STILL, **CODEX_REREAD})]),
    ("E26", "a new process finds the log rotated (new inode)",
     [Step("rotate", e26, resume="rejected", expect={**REG_STILL, **CODEX_REREAD})]),
    ("E27", "a new process finds the log truncated and written longer",
     [Step("truncate and rewrite", e27, resume="rejected",
           expect={**REG_STILL, **CODEX_REREAD})]),
    # E28/E29: caches keyed by (mtime, size) only; the content changes behind the key.
    ("E28", "a route .outcome.json sidecar rewritten with the same size and mtime",
     [Step("sidecar written", e28a, expect={**REG_STILL, **CODEX_STILL}),
      Step("same size and mtime, other content", e28b, expect={**REG_STILL, **CODEX_STILL})]),
    ("E29", "a claude transcript rewritten with the same size and mtime",
     [Step("same stat", e29, expect={**REG_STILL, **CODEX_STILL})]),
    # E34: the answer-wait candidates are built per registry segment and merged in file order.
    ("E34a", "an attempt row of route 0 appended",
     [Step("append", e34a, expect={**REG_APPEND, "generation_segment_builds": "+",
                                   "generation_segment_reuses": "+", **CODEX_STILL})]),
    ("E34b", "the same attempt appended several times",
     [Step("append x3", e34b, expect={**REG_APPEND, "generation_segment_builds": "+",
                                      "generation_segment_reuses": "+", **CODEX_STILL})]),
    ("E34c", "in place: an early row and a BLOCKED owner row change",
     [Step("two BLOCKED owners", blocked_setup, expect={**REG_APPEND, **CODEX_STILL}),
      Step("replace its row", e34c, expect={**REG_KEEP_PREFIX, "generation_segment_builds": "+",
                                            "generation_segment_reuses": "+", "segment_folds": "+",
                                            "segment_fold_reuses": "+", **CODEX_STILL})]),
    ("E34d", "an owner that began later on the BLOCKED owner's route",
     [Step("two BLOCKED owners", blocked_setup, expect={**REG_APPEND, **CODEX_STILL}),
      Step("later owner appended", e34d, expect={**REG_APPEND, "generation_segment_reuses": "+",
                                                 **CODEX_STILL})]),
    # E34e: a route whose rows sit in several segments, some with no usable time (activity nan).
    ("E34e", "one route over several segments, some rows without a usable time",
     [Step("first row without a time", e34e1, expect={**REG_APPEND, **CODEX_STILL}),
      Step("later row with a time", e34e2, expect={**REG_APPEND, "generation_segment_reuses": "+",
                                                   **CODEX_STILL}),
      Step("later row without a time", e34e3, expect={**REG_APPEND, "generation_segment_reuses": "+",
                                                      **CODEX_STILL})]),
    # E30/E31: caches keyed by (mtime, size) only that a route judgment reads through.
    ("E30", "a route record rewritten with the same size and mtime",
     [Step("same stat, other content", e30, expect={**REG_STILL, "route_wait_reuses": "+", **CODEX_STILL})]),
    ("E31", "a route-visibility outcome sidecar rewritten with the same size and mtime",
     [Step("sidecar written", e31a, expect={**REG_STILL, **CODEX_STILL}),
      Step("same size and mtime, other content", e31b, expect={**REG_STILL, **CODEX_STILL})]),
    # E32/E33/E35/E37: the S2 judgment of a route is computed again only when one of its inputs changed.
    ("E32a", "a raised gate appears in one route's journal",
     [Step("gate raised", e32a, expect={**REG_STILL, "route_wait_recomputes": 1,
                                        "route_wait_reuses": 2, **CODEX_STILL})]),
    ("E33", "one route's record file: replaced by the same bytes, deleted, written again",
     [Step("gate raised", e33_gate, expect={**REG_STILL, "route_wait_recomputes": 1, **CODEX_STILL}),
      Step("replaced by identical bytes", e33a, unchanged=True,
           expect={**REG_STILL, "route_wait_recomputes": "+", "route_wait_reuses": "+", **CODEX_STILL}),
      Step("deleted", e33b, expect={**REG_STILL, "route_wait_reuses": "+", **CODEX_STILL}),
      Step("written again", e33c, expect={**REG_STILL, "route_wait_recomputes": "+",
                                          "route_wait_reuses": "+", **CODEX_STILL})]),
    ("E37a", "the clock crosses the fold window, no route input changed",
     [Step("two BLOCKED owners near the window", blocked_near_fold_setup,
           expect={**REG_APPEND, **CODEX_STILL}),
      Step("+150 s", e37a, clock_only=True, expect={**REG_STILL, "route_wait_recomputes": "0",
                                                   "route_wait_reuses": "+", **CODEX_STILL})]),
    # E40: the owner-input decision (route verification) runs only for an owner that holds a kept
    # answer; with nothing kept the answer is [] whatever the decision says.
    ("E40", "a kept answer appears for a BLOCKED owner, then is consumed",
     [Step("two BLOCKED owners", blocked_setup, expect={**REG_APPEND, **CODEX_STILL}),
      Step("answer kept", e40a, expect={**REG_STILL, "owner_decisions": 1, "owner_decision_skips": "+",
                                        **CODEX_STILL}),
      Step("answer consumed", e40b, expect={**REG_STILL, "owner_decisions": "0", "owner_decision_skips": "+",
                                            **CODEX_STILL})]),
    # E40b: an unreadable journal only matters to an owner whose decision runs. Owner b holds nothing,
    # so it stays visible and unflagged; owner a holds an answer, so its decision raises as before.
    ("E40b", "an unreadable route journal: an owner without an answer is unaffected",
     [Step("two BLOCKED owners", blocked_setup, expect={**REG_APPEND, **CODEX_STILL}),
      Step("journal of an owner without an answer", e40c, unchanged=True,
           expect={**REG_STILL, "owner_decisions": "0", "owner_decision_skips": "+", **CODEX_STILL}),
      Step("journal of an owner holding an answer", e40d,
           expect={**REG_STILL, "owner_decisions": 1, **CODEX_STILL})]),
    # E42: a rewrite after the registry grew by appended batches (segments of unequal size). Only
    # the segments whose own bytes or first order changed are built again; the others keep their rows.
    # E43: who holds an owner row's job is remembered per row object and lineage graph. Rows that
    # did not change keep their answer, a changed row is asked again, a changed lineage asks every
    # row again, and the answers are the ones a process starting from nothing gives.
    ("E43", "owner holders: rows appended, a row edited in place, the lineage handed over",
     [Step("two BLOCKED owners", blocked_setup,
           expect={**REG_APPEND, "holder_resolves": 2, "holder_memo_hits": "+", **CODEX_STILL}),
      Step("owner row edited in place", e43_edit,
           expect={"full_metadata_parses": "+", "holder_resolves": "+", "holder_memo_hits": "+", **CODEX_STILL}),
      Step("holder handed over by the writer", e43_handover,
           expect={**REG_STILL, "holder_resolves": "+", "holder_memo_hits": "0", **CODEX_STILL})]),
    # E38: nothing a wait or an owner state is judged from lives in a kept value that a lease flock
    # or a session ending would leave stale: the lease change moves nothing, the ending moves the
    # owner states (liveness), and neither re-judges a route (inputs of every route are unchanged).
    ("E38", "a BLOCKED owner's supervisor lease is flocked and released, then the sessions end",
     [Step("two BLOCKED owners", blocked_setup, expect={**REG_APPEND, **CODEX_STILL}),
      Step("lease flocked and released", e38_lease, unchanged=True,
           expect={**REG_STILL, "route_wait_recomputes": "0", "route_wait_reuses": "+", **CODEX_STILL}),
      Step("sessions end", e38_gone,
           expect={**REG_STILL, "route_wait_recomputes": "0", "route_wait_reuses": "+", **CODEX_STILL})]),
    # E39: a registry replaced with one more owner row of another session reads the bytes as they are
    # and asks only the new row who holds its job; writing the same rows again as another inode
    # changes no answer and asks nobody.
    ("E39", "registry replaced: another session's owner row appears, then the same rows again",
     [Step("another session's owner row", e39_other_session,
           expect={"holder_resolves": 1, "holder_memo_hits": "+", **CODEX_STILL}),
      Step("same rows, another inode", e39_same, unchanged=True,
           expect={"holder_resolves": "0", "holder_memo_hits": "+", **CODEX_STILL})]),
    ("E42a", "in-place replacement of a middle row with a longer one, after appended batches",
     [Step("append batches", e42_batches, expect={**REG_APPEND, **CODEX_STILL}),
      Step("replace middle row", e42a, expect={**REG_KEEP_PREFIX, "segment_rebuilds": 1,
                                              "segment_resyncs": "+", **CODEX_STILL})]),
    ("E42b", "in-place replacement of two rows with the same lengths",
     [Step("append batches", e42_batches, expect={**REG_APPEND, **CODEX_STILL}),
      Step("replace two rows", e42b, expect={**REG_KEEP_PREFIX, "segment_rebuilds": 2,
                                            "segment_resyncs": "+", **CODEX_STILL})]),
    ("E42c", "an early row deleted: the order of every later row shifts",
     [Step("append batches", e42_batches, expect={**REG_APPEND, **CODEX_STILL}),
      Step("delete early row", e42c, expect={**REG_REPARSE, "segment_resyncs": "0",
                                             "segment_rebuilds": "+", **CODEX_STILL})]),
]


class ReadHook:
    """E11: append one row the instant the lowest read primitive has served the registry.

    It wraps the ``os`` name inside ``dispatch_registry_cache`` only, so every
    read the registry reader makes - whole-file or suffix - passes through it
    whichever path the warm side takes.  ``fired`` and ``via`` prove it ran.
    """

    def __init__(self, fx: Fixture, row: str):
        self.fx, self.row = fx, row
        self.fired, self.via = False, None
        info = fx.jobs.stat()
        self.identity = (info.st_dev, info.st_ino)

    def _after_read(self, name, fd, result):
        if self.fired or not result:
            return
        try:
            info = os.fstat(fd)
        except OSError:
            return
        if (info.st_dev, info.st_ino) != self.identity:
            return
        self.fired, self.via = True, name
        self.fx.append_rows([self.row])

    @contextlib.contextmanager
    def armed(self):
        import dispatch_registry_cache as cache
        hook, real = self, os

        class Proxy:
            def __getattr__(self, name):
                return getattr(real, name)

            def read(self, fd, size):
                result = real.read(fd, size)
                hook._after_read("os.read", fd, result)
                return result

            def pread(self, fd, size, offset):
                result = real.pread(fd, size, offset)
                hook._after_read("os.pread", fd, result)
                return result

        with mock.patch.object(cache, "os", Proxy()):
            yield self


class IncrementalEquivalenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temp = tempfile.TemporaryDirectory(prefix="incremental-equivalence-")
        cls.lab = Lab(Path(cls._temp.name))

    @classmethod
    def tearDownClass(cls):
        cls._temp.cleanup()

    # -- machinery --------------------------------------------------------
    def prime(self, eid: str) -> tuple[Run, dict]:
        run = Run(self.lab, eid)
        run.put_demand()
        run.warm()
        warm = run.warm()
        fresh = run.fresh()
        problem = run.mismatch("prime", warm, fresh)
        self.assertIsNone(problem, problem)
        return run, fresh

    def check_step(self, run: Run, step: Step, before: dict) -> dict:
        step.mutate(run)
        fresh = run.fresh()
        if step.resume:
            # Before the warm pass: it must not touch the cursor the new process reads.
            resumed = run.resume()
            problem = run.mismatch(step.label + " (resumed process)", resumed, fresh)
            self.assertIsNone(problem, problem)
            used = resumed["_meta"]["lifecycle_disk_resumes"]
            if step.resume == "used":
                self.assertGreaterEqual(
                    used, 1, "%s: the new process did not continue from the disk cursor, so "
                    "this event tested nothing" % run.eid)
            else:
                self.assertEqual(
                    used, 0, "%s: the new process continued from a disk cursor that no longer "
                    "matched the file" % run.eid)
        counted = counters()
        warm = run.warm()
        problem = run.mismatch(step.label, warm, fresh)
        self.assertIsNone(problem, problem)
        self.check_counters(run, step.label, counted, step.expect)
        # Self-check B: an event that cannot move the answer tests nothing.
        if step.unchanged:
            self.assertFalse(
                changed(before, fresh),
                "%s step '%s' moved the fresh output, which this step says it must not" % (
                    run.eid, step.label))
        else:
            self.assertTrue(
                changed(before, fresh),
                "%s step '%s' did not change the fresh output: the event tested nothing" % (
                    run.eid, step.label))
        counted = counters()
        again = run.warm()
        problem = run.mismatch(step.label + " (unchanged pass)", again, fresh)
        self.assertIsNone(problem, problem)
        still = dict(UNCHANGED_PASS)
        if run.fx.jobs.exists():
            still["cache_hits"] = "+"
        self.check_counters(run, step.label + " (unchanged pass)", counted, still)
        return fresh

    def check_counters(self, run: Run, label: str, before: dict, expect: dict) -> None:
        """The incremental path the event is about was really taken (or really not).

        ``expect`` maps a counter name to ``"+"`` (it must have grown during this warm
        pass) or ``"0"`` (it must not have moved).  A counter that is not listed is not
        judged, so an event names exactly what it is evidence of."""
        after = counters()
        delta = {name: after[name] - before[name] for name in after}
        if os.environ.get("EQUIV_SHOW_COUNTERS"):
            sys.stderr.write("COUNTERS %s %s %s\n" % (run.eid, label, _canonical(
                {k: v for k, v in delta.items() if v})))
        for name, want in sorted(expect.items()):
            got = delta[name]
            if want == "+":
                ok, text = got > 0, "grow"
            elif isinstance(want, int):
                ok, text = got == want, "move by exactly %d" % want
            else:
                ok, text = got == 0, "stay still"
            self.assertTrue(
                ok, "%s step '%s': counter %s must %s in the warm pass but moved by %d "
                "(all moved: %s) - the warm side did not take the path this event is about" % (
                    run.eid, label, name, text, got,
                    _canonical({k: v for k, v in delta.items() if v})))

    def run_event(self, eid: str, steps: list[Step]) -> None:
        run, fresh = self.prime(eid)
        for step in steps:
            fresh = self.check_step(run, step, fresh)

    # -- the events -------------------------------------------------------
    def test_events(self):
        only = {item for item in os.environ.get("EQUIV_EVENTS", "").split(",") if item}
        for eid, _description, steps in EVENTS:
            if only and eid not in only:      # a falsifier run names the events it is about
                continue
            with self.subTest(event=eid):
                self.run_event(eid, steps)

    def test_E11_append_while_reading(self):
        run, primed = self.prime("E11")
        run.fx.write_quota_log("att-quota-2", observed=CLOCK_BASE - 1800)
        run.fx.append_rows([run.fx.quota_row("att-quota-2", observed=CLOCK_BASE - 1800)])
        first = run.fresh()                                     # state with only the first row
        self.assertTrue(changed(primed, first), "E11 first append did not change the output")
        hook = ReadHook(run.fx, _new_open_row(run.fx, "during-read"))
        with hook.armed():
            warm = run.warm()
        self.assertTrue(hook.fired, "E11 injection did not fire: the append never happened "
                        "during a read, so this test checked nothing")
        both = run.fresh()                                      # state with both rows
        self.assertTrue(changed(first, both), "E11 second append did not change the output")
        if first_difference(warm, first) is not None and first_difference(warm, both) is not None:
            problem = run.mismatch("read during append (neither state)", warm, both)
            self.fail(problem + " [injected via %s]" % hook.via)
        again = run.warm()
        problem = run.mismatch("unchanged pass after the append", again, both)
        self.assertIsNone(problem, problem)

    # -- self-checks ------------------------------------------------------
    def test_self_check_a_fresh_runs_repeat(self):
        """Same inputs, same clock, two new processes: the bytes must match.

        If they do not, a real clock or the environment leaks into the output and
        every later comparison would blame the incremental path for it.
        """
        run = Run(self.lab, "SELF-A")
        run.put_demand()
        first, second = run.fresh(), run.fresh()
        found = first_difference(first, second)
        self.assertIsNone(
            found, "SELF-A %s: two fresh runs of the same fixture and clock differ "
            "(%s vs %s); look for time.time/datetime.now/os.getpid readers not covered by "
            "FixtureClock (grep 'datetime.now\\|time.time()' in the module named by the pointer)" % (
                found[0] if found else "", brief(found[1]) if found else "",
                brief(found[2]) if found else ""))

    def test_self_check_b_a_noop_event_is_caught(self):
        """The 'did this event change anything' check must be able to say no."""
        run, fresh = self.prime("SELF-B")
        again = run.fresh()
        self.assertFalse(changed(fresh, again))
        run.clock.advance(120)
        self.assertTrue(changed(fresh, run.fresh()), "a moved clock must move the output")

    def test_comparison_sees_a_difference_and_allows_nothing_silently(self):
        self.assertIsNone(first_difference({"a": [1, {"b": 2}]}, {"a": [1, {"b": 2}]}))
        pointer, left, right = first_difference({"a": [1, {"b": 2}]}, {"a": [1, {"b": 3}]})
        self.assertEqual((pointer, left, right), ("/a/1/b", 2, 3))
        self.assertEqual(first_difference({"a": 1}, {})[0], "/a")
        self.assertEqual(first_difference({"a": [1]}, {"a": [1, 2]})[0], "/a/1")
        for pattern, reason in ALLOWED:
            self.assertTrue(reason.strip(), "every allow-list entry needs its reason")

    def test_stale_registry_reuse_is_caught(self):
        """The harness itself: a reader that keeps the first view must fail E01."""
        import dispatch_registry_cache as cache
        run, fresh = self.prime("SELF-STALE")
        real = cache._registry_view_fresh
        first = real(run.fx.jobs)

        def stale(jobs, *, append_only=False):
            return first

        with mock.patch.object(cache, "_registry_view_fresh", stale):
            run.fx.append_rows([_new_open_row(run.fx, "stale-probe")])
            after = run.fresh()
            warm = run.warm()
        self.assertTrue(changed(fresh, after))
        self.assertIsNotNone(first_difference(warm, after),
                             "a warm reader frozen on the first view was not noticed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--compute":
        raise SystemExit(main_child(sys.argv[2:]))
    unittest.main()
