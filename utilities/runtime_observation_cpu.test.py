#!/usr/bin/env python3
"""Isolated production-entry CPU and invalidation fixture for runtime observers."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pstats
import resource
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import tarfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest import mock


SOURCE_ROOT = Path(__file__).resolve().parents[1]
JOBS_BYTES = 23_800_000
INDEX_BYTES = 91_000_000
MANIFEST_BYTES = 1_400_000
DIAGNOSTIC_PROFILE = os.environ.get("HEARTING_CPU_DIAGNOSTIC") == "profile"
BASELINE_MODE = os.environ.get("HEARTING_CPU_BASELINE") == "1"
OBSERVER_COUNT = 1 if DIAGNOSTIC_PROFILE else 8
WATCHER_COUNT = 1 if DIAGNOSTIC_PROFILE else 11
RUNNER_COUNT = 1
IDLE_SECONDS = 3 if DIAGNOSTIC_PROFILE else 20
BURST_SECONDS = 3 if DIAGNOSTIC_PROFILE else 20
COLD_SECONDS = 5 if DIAGNOSTIC_PROFILE else 10

_RUNNER_DIAGNOSTICS = r'''
import faulthandler, os, pathlib, signal, sys, traceback
if "peer-steward.py" in sys.argv[0] and "__obligation-runner" in sys.argv:
    directory = pathlib.Path(os.environ["FIXTURE_RUNNER_DIAGNOSTICS"])
    diagnostic_stream = (directory / (str(os.getpid()) + ".log")).open("a", buffering=1)
    faulthandler.enable(file=diagnostic_stream)
    faulthandler.register(signal.SIGUSR1, file=diagnostic_stream, all_threads=True)
    def diagnostic_exception(kind, error, tb):
        traceback.print_exception(kind, error, tb, file=diagnostic_stream)
    sys.excepthook = diagnostic_exception
    (directory / (str(os.getpid()) + ".ready")).write_text("ready")
'''


_FAKE_HERDR = r'''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
if argv[:1] == ["--session"]:
    argv = argv[2:]
verb = argv[1] if len(argv) > 1 else ""
target = argv[2] if len(argv) > 2 else "fixture-target"
log = os.environ["FIXTURE_HERDR_LOG"]
with open(log, "a", encoding="utf-8") as out:
    out.write(f"{os.getpid()} {verb} {target}\n")
payload = {"id": "fixture-herdr", "result": {"agent": {
    "agent": "claude", "agent_session": {"value": "sid-fixture"},
    "agent_status": "idle", "name": target, "pane_id": "w1:p9"
}, "type": "agent_info"}}
if verb == "process-info":
    worker = int(os.environ["FIXTURE_WATCH_FOREGROUND_PID"])
    payload["result"] = {"process_info": {
        "pane_id": "w1:p9", "shell_pid": int(os.environ["FIXTURE_WATCH_SHELL_PID"]),
        "foreground_process_group_id": worker,
        "foreground_processes": [{"pid": worker, "argv": [sys.executable]}]}}
print(json.dumps(payload))
'''


class FakeObservationEndpoint:
    """Small newline JSON server implementing the two observed protocol methods."""

    def __init__(self, path: Path, panes: list[dict], agents: list[dict] | None = None):
        self.path = path
        self.panes = panes
        self.agents = list(panes if agents is None else agents)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen(64)
        self.listener.settimeout(0.1)
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.subscriptions: list[socket.socket] = []
        self.requests = {"events.subscribe": 0, "session.snapshot": 0}
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    @staticmethod
    def _read_line(connection):
        data = bytearray()
        while b"\n" not in data:
            chunk = connection.recv(65536)
            if not chunk:
                return None
            data.extend(chunk)
        return json.loads(bytes(data).split(b"\n", 1)[0])

    @staticmethod
    def _send(connection, payload):
        connection.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode())

    def _serve(self, connection):
        try:
            request = self._read_line(connection)
            if not isinstance(request, dict):
                return
            method = request.get("method")
            with self.lock:
                self.requests[method] = self.requests.get(method, 0) + 1
            response = {"id": request.get("id")}
            if method == "events.subscribe":
                response["result"] = {"subscribed": True}
                self._send(connection, response)
                connection.settimeout(0.2)
                with self.lock:
                    self.subscriptions.append(connection)
                while not self.stop.is_set():
                    try:
                        if not connection.recv(1):
                            break
                    except socket.timeout:
                        continue
            elif method == "session.snapshot":
                response["result"] = {"type": "session_snapshot", "snapshot": {
                    "protocol": 22, "panes": self.panes, "agents": self.agents}}
                self._send(connection, response)
            else:
                response["error"] = {"code": "unknown_method"}
                self._send(connection, response)
        except (OSError, ValueError, TypeError):
            pass
        finally:
            with self.lock:
                if connection in self.subscriptions:
                    self.subscriptions.remove(connection)
            with contextlib.suppress(OSError):
                connection.close()

    def _accept(self):
        while not self.stop.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def publish_unrelated(self):
        frame = (json.dumps({"event": "pane.updated", "data": {
            "pane_id": "w99:p999", "pane": {"pane_id": "w99:p999", "agent": "claude"}
        }}, separators=(",", ":")) + "\n").encode()
        failed = []
        with self.lock:
            for connection in self.subscriptions:
                try:
                    connection.sendall(frame)
                except OSError:
                    failed.append(connection)
            for connection in failed:
                self.subscriptions.remove(connection)

    def close(self):
        self.stop.set()
        with contextlib.suppress(OSError):
            self.listener.close()
        with self.lock:
            connections = list(self.subscriptions)
        for connection in connections:
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
                connection.close()
        self.thread.join(timeout=1)


class FakeControlEndpoint:
    """A live busy recipient: real observer normalization, no delivered turn."""
    def __init__(self, path: Path):
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen(32)
        self.listener.settimeout(.1)
        self.stop = threading.Event()
        self.deliveries = set()
        self.requests = 0
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self.stop.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                try:
                    connection.settimeout(2)
                    request = FakeObservationEndpoint._read_line(connection)
                    if not isinstance(request, dict):
                        continue
                    self.requests += 1
                    if request.get("op") == "status":
                        response = {"schema_version": 1, "status": "ready", "capabilities": {
                            "human_gate_delivery": {"version": 1, "thread_id": "fixture-parent", "epoch": 1}}}
                    else:
                        self.deliveries.update(child["attempt_id"]
                            for child in (request.get("receipt") or {}).get("children", []))
                        response = {"schema_version": 1, "status": "retryable", "reason": "fixture-recipient-busy"}
                    FakeObservationEndpoint._send(connection, response)
                except (OSError, ValueError, TypeError, KeyError):
                    continue

    def close(self):
        self.stop.set()
        self.listener.close()
        self.thread.join(timeout=2)


def _import_from(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _proc_stat(pid: int):
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        tail = raw[raw.rfind(")") + 2:].split()
        if not tail or tail[0] in {"Z", "X"}:
            return None
        return {
            "pid": pid, "ppid": int(tail[1]), "pgrp": int(tail[2]),
            "utime": int(tail[11]), "stime": int(tail[12]),
            "cutime": int(tail[13]), "cstime": int(tail[14]),
            "start": tail[19], "rss_pages": int(tail[21]),
        }
    except (OSError, ValueError, IndexError):
        return None


def _process_tree_snapshot(seed_pids: set[int]):
    rows = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        item = _proc_stat(int(name))
        if item is not None:
            rows[item["pid"]] = item
    groups = {rows[pid]["pgrp"] for pid in seed_pids if pid in rows}
    selected = {pid for pid, item in rows.items()
                if pid in seed_pids or item["pgrp"] in groups}
    ancestors = set(seed_pids)
    changed = True
    while changed:
        changed = False
        for pid, item in rows.items():
            if item["ppid"] in ancestors and pid not in selected:
                selected.add(pid)
                ancestors.add(pid)
                changed = True
    result = {}
    for pid in selected:
        item = rows.get(pid)
        if item is None:
            continue
        key = (pid, item["start"])
        try:
            io = Path(f"/proc/{pid}/io").read_text().splitlines()
            rchar = int(next(line.split()[1] for line in io if line.startswith("rchar:")))
        except (OSError, ValueError, StopIteration):
            rchar = 0
        item["rchar"] = rchar
        result[key] = item
    return result


def _sample_window(seconds: float, seed_pids: set[int], seed_provider=None):
    start = time.monotonic()
    start_epoch = time.time()
    sampler_start = _proc_stat(os.getpid())
    refresh_roots_at = start
    previous = _process_tree_snapshot(seed_pids)
    hz = os.sysconf("SC_CLK_TCK")
    def inclusive_cpu(snapshot):
        reaped = resource.getrusage(resource.RUSAGE_CHILDREN)
        return (reaped.ru_utime + reaped.ru_stime) * hz + sum(
            item["utime"] + item["stime"] + item["cutime"] + item["cstime"]
            for item in snapshot.values())
    inclusive_before = inclusive_cpu(previous)
    ticks = 0
    read_chars = 0
    max_rss = 0
    peak_processes = len(previous)
    identities = set(previous)
    while time.monotonic() - start < seconds:
        time.sleep(0.05)
        now = time.monotonic()
        if seed_provider is not None and now >= refresh_roots_at:
            seed_pids.update(seed_provider())
            refresh_roots_at = now + 1.0
        current = _process_tree_snapshot(seed_pids)
        peak_processes = max(peak_processes, len(current))
        identities.update(current)
        for key, item in current.items():
            before = previous.get(key)
            if before is None:
                ticks += item["utime"] + item["stime"]
                read_chars += item["rchar"]
            else:
                ticks += max(0, item["utime"] + item["stime"]
                             - before["utime"] - before["stime"])
                read_chars += max(0, item["rchar"] - before["rchar"])
            max_rss = max(max_rss, item["rss_pages"] * os.sysconf("SC_PAGE_SIZE"))
        previous = current
    elapsed = time.monotonic() - start
    end_epoch = time.time()
    inclusive_ticks = max(0, inclusive_cpu(previous) - inclusive_before)
    sampler_end = _proc_stat(os.getpid())
    sampler_ticks = 0
    if sampler_start is not None and sampler_end is not None:
        sampler_ticks = max(0, sampler_end["utime"] + sampler_end["stime"]
                            - sampler_start["utime"] - sampler_start["stime"])
    return {
        "elapsed_seconds": elapsed, "start_epoch": start_epoch,
        "end_epoch": end_epoch, "cpu_ticks": inclusive_ticks,
        "sampled_own_cpu_ticks": ticks,
        "cpu_accounting": "live own+waited-child ticks plus driver RUSAGE_CHILDREN; no retained dead-PID double count",
        "cpu_percent_one_core": 100.0 * inclusive_ticks / (hz * elapsed),
        "sampler_cpu_ticks": sampler_ticks,
        "sampler_cpu_percent_one_core": 100.0 * sampler_ticks / (hz * elapsed),
        "rchar_bytes": read_chars, "max_sampled_rss_bytes": max_rss,
        "peak_processes": peak_processes,
        "pid_start_identities": [list(item) for item in sorted(identities)],
    }


def _make_source_copy(destination: Path):
    shutil.copytree(
        SOURCE_ROOT, destination,
        ignore=shutil.ignore_patterns(
            ".git", ".agent_reports", ".claude_reports", ".dispatch",
            "__pycache__", "node_modules", "dist", "coverage"),
        symlinks=True,
    )
    if not (destination / "RELEASE_VERSION").is_file():
        (destination / "RELEASE_VERSION").write_text("fixture\n", encoding="utf-8")
    if not (destination / "harness-manifest.json").is_file():
        (destination / "harness-manifest.json").write_text("{}\n", encoding="utf-8")


def _valid_project(project_root: Path, *, large_manifest: bool,
                   large_index: bool, modules: dict):
    admission = modules["admission"]
    identity = admission.ensure_root_identity(project_root)
    fixture = modules["admission_fixture"]
    allocator = modules["identity"].IdAllocator()
    doc, content = fixture._make_valid_document(allocator, identity, content=b"payload\n")
    manifest = modules["manifest"]
    target_rows = 3200 if large_manifest else 8
    digest = manifest.digest_bytes(content)
    provenance = {
        "source_manifest_id": doc["manifest_id"],
        "source_revision_id": doc["manifest_revision_id"],
        "producer_route_id": "fixture-route",
        "algorithm_version": "fixture-v1",
        "schema_version": 1,
        "source_digest": "sha256:" + "2" * 64,
    }
    for index in range(1, target_rows):
        artifact_id = allocator.allocate("artifact")
        revision_id = allocator.allocate("artifact_revision")
        doc["artifacts"].append({
            "artifact_id": artifact_id, "cycle_id": doc["cycle"]["cycle_id"],
            "role": "supporting", "type": "doc", "capability": "fixture",
            "title": f"payload-{index}",
        })
        doc["artifact_revisions"].append({
            "artifact_revision_id": revision_id, "artifact_id": artifact_id,
            "revision_sequence": 1, "content_digest": digest,
            "byte_size": len(content), "media_type": "text/plain",
            "locator": {"kind": "cycle-relative", "path": f"artifacts/payload-{index:05d}.txt"},
            "provenance": dict(provenance),
        })
    report = manifest.validate(doc)
    if not report.ok:
        raise AssertionError(f"generated manifest is invalid: {report.to_payload()}")
    raw_manifest = manifest.canonical_bytes(doc)
    if large_manifest and len(raw_manifest) < MANIFEST_BYTES:
        raise AssertionError(f"generated manifest is only {len(raw_manifest)} bytes")
    directory = project_root / "campaigns" / doc["campaign"]["campaign_id"] \
        / "cycles" / doc["cycle"]["cycle_id"]
    payload_dir = directory / "artifacts"
    payload_dir.mkdir(parents=True)
    for index in range(1, target_rows):
        (payload_dir / f"payload-{index:05d}.txt").write_bytes(content)
    (directory / "plan.md").write_bytes(content)
    (directory / "manifest.json").write_bytes(raw_manifest)

    cycle_path = os.path.relpath(directory, project_root)
    index_payload = {
        "schema_version": 1, "artifact_root_id": identity.artifact_root_id,
        "stable_ids": {}, "routes": {}, "event_ids": {}, "streams": {},
        "manifests": {doc["cycle"]["cycle_id"]: {
            "manifest_id": doc["manifest_id"],
            "manifest_revision_id": doc["manifest_revision_id"],
            "cycle_id": doc["cycle"]["cycle_id"],
            "manifest_digest": manifest.manifest_digest(doc),
            "idempotency_key": doc["cycle"]["cycle_id"],
        }},
        "cycles": {doc["cycle"]["cycle_id"]: {
            "campaign_id": doc["campaign"]["campaign_id"],
            "cycle_path": cycle_path,
            "manifest_digest": manifest.manifest_digest(doc),
        }},
    }
    for row in doc["artifacts"]:
        index_payload["stable_ids"][row["artifact_id"]] = {
            "kind": "artifact", "cycle_id": doc["cycle"]["cycle_id"],
            "manifest_id": doc["manifest_id"],
        }
    if large_index:
        cycle_id = doc["cycle"]["cycle_id"]
        manifest_id = doc["manifest_id"]
        count = 560_000
        for number in range(count):
            key = f"artifact_{number:032x}"
            index_payload["stable_ids"].setdefault(key, {
                "kind": "artifact", "cycle_id": cycle_id,
                "manifest_id": manifest_id,
            })
    admission_dir = project_root / ".runtime" / "artifact-admission" / "v1"
    admission_dir.mkdir(parents=True, exist_ok=True)
    raw_index = json.dumps(index_payload, sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False).encode()
    if large_index and len(raw_index) < INDEX_BYTES:
        raise AssertionError(f"generated index is only {len(raw_index)} bytes")
    (admission_dir / "index.json").write_bytes(raw_index + b"\n")
    loaded = admission.load_index(project_root)
    if len(loaded.stable_ids) < target_rows:
        raise AssertionError("generated index did not retain its stable-id rows")
    return {
        "root": str(project_root), "artifact_root_id": identity.artifact_root_id,
        "campaign_id": doc["campaign"]["campaign_id"],
        "cycle_id": doc["cycle"]["cycle_id"], "manifest_id": doc["manifest_id"],
        "manifest_bytes": len(raw_manifest), "index_bytes": len(raw_index) + 1,
        "manifest_validation": "passed", "index_rows": len(loaded.stable_ids),
    }


def _valid_sealed_project(project_root: Path, temp_root: Path, modules: dict):
    """Create a real completed producer cycle and verify its sealed history."""
    producer_fixture = modules["producer_fixture"]
    producer = modules["producer"]
    admission = modules["admission"]
    artifact_index = modules["artifact_index"]
    project_root.mkdir(parents=True, exist_ok=True)
    producer.activate(
        project_root, repository_id=producer_fixture.REPO_ID,
        artifact_root_id=producer_fixture.ROOT_ID,
        w7={"campaign_id": "camp_" + "c" * 32},
    )
    route = producer_fixture.compile_for(
        "direct", project_root, slug="runtime-observation-cpu-fixture")
    binding = producer_fixture.L.admit_runtime_route(project_root, route)
    route_file = Path(binding.route_file)
    begun = producer.begin(
        project_root, route_file=route_file, capability=route["capability"],
        intensity=route["effective_intensity"],
    )
    cycle_dir = Path(begun["cycle_dir"])
    payload_dir = cycle_dir / "artifacts"
    payload_dir.mkdir(parents=True, exist_ok=True)
    payload = b"runtime observation fixture payload\n"
    for index in range(3200):
        (payload_dir / f"fixture-payload-{index:05d}.txt").write_bytes(payload)

    evidence = temp_root / "terminal-evidence.md"
    evidence.write_text("isolated completed-cycle fixture evidence\n", encoding="utf-8")
    for node in route["nodes"]:
        if node.get("terminal") is not True:
            continue
        if node.get("dispatch_depth", 0) == 0:
            producer_fixture.R.write_completion_marker(route, node, node["id"], evidence)
        else:
            metadata = {
                "attempt_schema_version": 2, "dispatch_depth": node["dispatch_depth"],
                "transport": "headless", "execution_surface": "registered-headless",
                "registered_worker": 1, "fallback_hop": "same-harness-headless",
            }
            producer_fixture.R.write_completion_marker(
                route, node, node["id"], evidence,
                attempt_id=f"att-cpu-fixture-{node['id']}", attempt_metadata=metadata)
    outcome, _generation = producer_fixture.R.close_route(
        route, route_file, commit="a" * 40, summary="isolated CPU fixture")
    if not outcome.get("terminal_gate_proven"):
        raise AssertionError(f"fixture route did not close: {outcome}")
    sealed = producer.finalize(project_root, cycle_id=begun["cycle_id"])
    if sealed.get("status") not in {"sealed", "already-sealed"}:
        raise AssertionError(f"fixture cycle did not seal: {sealed}")

    record = producer.read_cycle_record(project_root, begun["cycle_id"])
    manifest_path = producer._record_cycle_manifest_path(project_root, record)
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_bytes = manifest_path.stat().st_size
    if manifest_bytes < MANIFEST_BYTES:
        raise AssertionError(f"producer manifest is only {manifest_bytes} bytes")

    index_doc = admission.load_index(project_root)
    payload_doc = artifact_index.to_payload(index_doc)
    cycle_id = begun["cycle_id"]
    manifest_id = index_doc.manifests[cycle_id]["manifest_id"]
    stable_ids = payload_doc["stable_ids"]
    stable_row = {"kind": "artifact", "cycle_id": cycle_id,
                  "manifest_id": manifest_id}
    for number in range(560_000):
        stable_ids.setdefault(f"artifact_{number:032x}", stable_row)
    raw_index = json.dumps(payload_doc, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False).encode()
    if len(raw_index) < INDEX_BYTES:
        raise AssertionError(f"inflated schema-valid index is only {len(raw_index)} bytes")
    index_path = admission._index_path(project_root)
    index_path.write_bytes(raw_index + b"\n")

    context = getattr(producer, "_HISTORICAL_OBSERVATIONS", None)
    if context is not None:
        context.clear()
        before = context.metrics_snapshot()
    expected = {"route_id": route["route_id"], "route_hash": route["route_hash"]}
    verified = producer._verify_sealed_cycle_locked(
        project_root, record, expected_binding=expected, historical=True)
    if verified.get("status") != "already-sealed":
        raise AssertionError(f"historical sealed-cycle verification failed: {verified}")
    if context is not None:
        producer._verify_sealed_cycle_locked(
            project_root, record, expected_binding=expected, historical=True)
        after = context.metrics_snapshot()
        verification_metrics = {key: after[key] - before[key] for key in after}
    else:
        verification_metrics = {"legacy_historical_verify_calls": 1}

    return {
        "root": str(project_root),
        "artifact_root_id": admission.ensure_root_identity(project_root).artifact_root_id,
        "campaign_id": document["campaign"]["campaign_id"],
        "cycle_id": cycle_id, "manifest_id": manifest_id,
        "manifest_revision_id": document["manifest_revision_id"],
        "manifest_path": str(manifest_path),
        "manifest_bytes": manifest_bytes,
        "index_bytes": index_path.stat().st_size,
        "manifest_validation": "producer-sealed-and-historically-verified",
        "index_rows": len(stable_ids),
        "historical_verification": verified,
        "historical_verification_metrics_delta": verification_metrics,
    }


def _settled_owner_projects(base: Path, jobs: Path, modules: dict, *, count=16,
                            large_first=True):
    """Actual sealed owner transactions, not done labels on unrelated stage rows."""
    producer_fixture = modules["producer_fixture"]
    producer = modules["producer"]
    admission = modules["admission"]
    index_module = modules["artifact_index"]
    contract = __import__("dispatch_contract")
    terminal = __import__("dispatch_terminal_commit")
    join = __import__("dispatch_completion_join")
    projects, registry_rows = [], []
    for number in range(count):
        directory = base / f"settled-owner-{number:02d}"
        directory.mkdir(parents=True)
        fixture = producer_fixture.TerminalTransactionIntegrationTest()
        fixture.root = directory / ".agent_reports"
        fixture.root.mkdir()
        fixture._tmp = SimpleNamespace(name=str(directory))
        harness = ("claude", "codex", "opencode")[number % 3]
        owner = f"att-settled-history-owner-{number:02d}"
        child = f"att-settled-history-child-{number:02d}"
        route, route_file, local_jobs, _, cycle, report, request = fixture._prepare_fixture(
            harness, "autopilot-code", owner=owner, child=child)
        if number == 0 and large_first:
            payload_dir = Path(cycle["cycle_dir"]) / "artifacts" / "history-payloads"
            payload_dir.mkdir(parents=True)
            for index in range(3200):
                (payload_dir / f"payload-{index:05d}.txt").write_bytes(b"sealed historical payload\n")
        text = f"artifact: {report}\nverdict: PASS\nblocker: none"
        native = {
            "claude": [{"type": "result", "subtype": "success", "is_error": False, "result": text}],
            "codex": [{"type": "item.completed", "item": {"type": "agent_message", "text": text}},
                      {"type": "turn.completed"}],
            "opencode": [{"type": "text", "sessionID": "ses_fixture", "part": {"type": "text", "text": text}},
                         {"type": "step_finish", "sessionID": "ses_fixture",
                          "part": {"type": "step-finish", "reason": "stop"}}],
        }[harness]
        log = directory / "owner.jsonl"
        log.write_text("\n".join(json.dumps(item) for item in native) + "\n")
        local_jobs.write_text(local_jobs.read_text().replace("worker_type=owner",
            f"attempt_schema_version=2,worker_type=owner,log_file={log},workflow_completion=runtime-v1"))
        fixture._closed_owner(local_jobs, owner)
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(local_jobs),
                                          "AGENT_ARTIFACT_ROOT": str(fixture.root)}):
            meta = join.exact_attempt_row(local_jobs, owner).metadata
            settled = terminal.settle_owner_completion(local_jobs, "done", meta)
            if settled.result != "completed":
                raise AssertionError(f"history owner did not settle: {settled}")
            record = producer.read_cycle_record(fixture.root, cycle["cycle_id"])
            manifest_path = producer._record_cycle_manifest_path(fixture.root, record)
            if number == 0 and large_first:
                index_doc = index_module.to_payload(admission.load_index(fixture.root))
                stable_row = {"kind": "artifact", "cycle_id": cycle["cycle_id"],
                              "manifest_id": index_doc["manifests"][cycle["cycle_id"]]["manifest_id"]}
                for index in range(560_000):
                    index_doc["stable_ids"].setdefault(f"artifact_{index:032x}", stable_row)
                admission._index_path(fixture.root).write_text(
                    json.dumps(index_doc, sort_keys=True, separators=(",", ":")) + "\n")
                del index_doc
        old_completion = producer_fixture.R.completion_dir(route["route_id"], jobs=local_jobs)
        new_completion = producer_fixture.R.completion_dir(route["route_id"], jobs=jobs)
        shutil.copytree(old_completion, new_completion, dirs_exist_ok=True)
        producer_fixture.R._rewrite_migrated_attempt_links(new_completion, old_completion, new_completion)
        claim = contract.terminal_claim_path(local_jobs, route["route_id"], owner)
        destination = contract.terminal_claim_path(jobs, route["route_id"], owner)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(claim, destination)
        for line in local_jobs.read_text().splitlines():
            line = line.replace(str(old_completion), str(new_completion))
            fields = line.split("\t")
            meta = contract.parse_registry_metadata(fields[5])
            if meta.get("attempt_id") == owner:
                fields[5] += (",launch_claimed=1,launch_started=1,supervisor_lease=flock-v1,supervisor_lease_file="
                              + str(contract.supervisor_lease_path(jobs, owner)))
            registry_rows.append("\t".join(fields) + "\n")
        projects.append({"root": str(fixture.root), "owner_attempt_id": owner,
                         "harness": harness, "cycle_id": cycle["cycle_id"],
                         "manifest_path": str(manifest_path), "manifest_bytes": manifest_path.stat().st_size,
                         "index_bytes": admission._index_path(fixture.root).stat().st_size})
    with jobs.open("a") as out:
        out.writelines(registry_rows)
    # Verify the transplanted canonical registry/claim/marker graph through
    # the actual public historical judgment before using it as workload.
    for project in projects:
        meta = join.exact_attempt_row(jobs, project["owner_attempt_id"]).metadata
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs)}):
            state = terminal.owner_completion_state(jobs, "done", meta)
        if state.state != "complete":
            raise AssertionError(f"history fixture is not authoritative: {project['owner_attempt_id']} {state}")
    return projects


_FOREGROUND_WAITER = r'''
import dataclasses, json, os, pathlib, subprocess, sys
import dispatch_contract as D
from dispatch_lifecycle import wait_foreground
jobs, attempt, ready, result, child_script = sys.argv[1:]
binding = D.resolve_live_parent_attempt(pathlib.Path(jobs), parent_slug=attempt,
    repo=os.environ["AGENT_HOME"], worktree=os.environ["AGENT_HOME"], expected_attempt_id=attempt)
child = subprocess.Popen([sys.executable, child_script], start_new_session=True)
pathlib.Path(ready).write_text(json.dumps({"child_pid": child.pid, "parent_attempt": attempt}))
outcome = wait_foreground(child, 300, parent_is_live=lambda: D.parent_attempt_binding_is_live(jobs, binding))
pathlib.Path(result).write_text(json.dumps(dataclasses.asdict(outcome)))
'''


_REGISTRATION_PROBE = r'''
import json, os, pathlib, signal, sys, time
import dispatch_contract as D
jobs, parent, historical_owner = sys.argv[1:]
jobs = pathlib.Path(jobs)
binding = D.resolve_live_parent_attempt(jobs, parent_slug=parent,
    repo=os.environ["AGENT_HOME"], worktree=os.environ["AGENT_HOME"], expected_attempt_id=parent)
real_flock = D.fcntl.flock
calls = []
def flock(fd, operation):
    path = os.path.realpath(os.readlink(f"/proc/self/fd/{fd}"))
    if path == str(jobs) + ".lock" and operation & D.fcntl.LOCK_EX:
        held_other_fds = []
        if not D._terminal_claim_lock_owned(fd, jobs):
            for name in os.listdir("/proc/self/fd"):
                if name.isdigit() and int(name) != fd and D._terminal_claim_lock_owned(int(name), jobs):
                    held_other_fds.append(int(name))
        call = {"fd": fd, "requested": time.monotonic(), "acquired": None,
                "held_other_fds": held_other_fds}
        calls.append(call)
        value = real_flock(fd, operation)
        call["acquired"] = time.monotonic()
        return value
    return real_flock(fd, operation)
D.fcntl.flock = flock
def expired(*_):
    raise TimeoutError("registration-lock-deadline")
signal.signal(signal.SIGALRM, expired)
preclaim_calls = 0
def preclaim(lines):
    global preclaim_calls
    preclaim_calls += 1
    if not D.parent_attempt_binding_is_live(jobs, binding):
        raise AssertionError("exact parent lost while registering")
    # Both public observations must be safe inside a normal registration's
    # existing critical section, including a closed historical owner.
    D.probe_owner_supervision(jobs, historical_owner)
aid = "att-fixture-registration-probe"
row = ("now\topen\t" + os.environ["AGENT_HOME"] + "\t" + os.environ["AGENT_HOME"]
       + "\tregistration-probe\tattempt_schema_version=2,dispatch_depth=2,transport=headless,"
       "execution_surface=registered-headless,registered_worker=1,fallback_hop=same-harness-headless,"
       "attempt_id=" + aid)
start = time.monotonic()
signal.alarm(4)
try:
    registered = D.claim_attempt_row(jobs, aid, row, launch=True, preclaim=preclaim)
    if preclaim_calls != 1:
        raise AssertionError("registration did not exercise its locked observations")
    result = {"registered": registered, "state": "returned"}
except TimeoutError:
    result = {"registered": False, "state": "lock-timeout"}
except AssertionError as exc:
    # Older observations may turn the alarm's OSError into a false liveness
    # result. Keep the exact lock evidence instead of losing it to a JSON
    # decode error in the fixture driver. The after gate still rejects this.
    result = {"registered": False, "state": "observation-failed", "error": str(exc)}
finally:
    signal.alarm(0)
end = time.monotonic()
for call in calls:
    call["acquire_wait_seconds"] = None if call["acquired"] is None else call["acquired"] - call["requested"]
    call["hold_seconds_upper_bound"] = None if call["acquired"] is None else end - call["acquired"]
result.update(elapsed_seconds=end-start, jobs_lock_requests=calls,
              preclaim_calls=preclaim_calls,
              nested_lock_request=any(call["held_other_fds"] for call in calls))
print(json.dumps(result), flush=True)
'''


def _start_foreground_waiters(root: Path, jobs: Path, source: Path, contract):
    waiter = root / "foreground-waiter.py"
    child = root / "foreground-child.py"
    waiter.write_text(_FOREGROUND_WAITER)
    child.write_text("import time; time.sleep(600)\n")
    running = []
    for index in range(4):
        parent = subprocess.Popen([sys.executable, str(child)], start_new_session=True,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        attempt = f"att-foreground-parent-{index}"
        identity = contract.process_launch_identity(parent.pid)
        row = (f"now\topen\t{source}\t{source}\t{attempt}\t"
               "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
               "execution_surface=registered-headless,registered_worker=1,"
               "fallback_hop=same-harness-headless,worker_type=owner,"
               f"attempt_id={attempt},harness=codex,"
               + ",".join(f"{key}={value}" for key, value in identity.items()) + "\n")
        with jobs.open("a") as out:
            out.write(row)
        ready, result = root / f"foreground-{index}.ready.json", root / f"foreground-{index}.result.json"
        log = (root / f"foreground-{index}.log").open("w")
        try:
            process = subprocess.Popen([sys.executable, str(waiter), str(jobs), attempt,
                str(ready), str(result), str(child)], stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        finally:
            log.close()
        running.append((process, parent, ready, result, attempt))
    return running


def _hook_delivery_roundtrip(source: Path, jobs: Path):
    pending = __import__("dispatch_pending_delivery")
    sweep = __import__("dispatch_session_sweep")
    from dispatch_recipient_receipt import receipt_marker
    session = "fixture-hook-recipient"
    aid = "att-fixture-hook-notice"
    delivery_id = "delivery-" + "d" * 32
    receipt = {"schema_version": 2, "state": "ready", "parent_attempt_id": "att-fixture-hook-parent",
               "job_registry": str(jobs), "delivery_classification": "attention",
               "children": [{"attempt_id": aid, "status": "done", "readiness": "ready",
                             "reason": "terminal-failure-or-unclosed", "required_action": "inspect-done-failure",
                             "delivery_classification": "attention"}]}
    original = pending.create(root=jobs.parent, recipient_kind="claude-parent-runtime", recipient_key=session,
        delivery_id=delivery_id, session_generation="", session_generation_supported="0",
        attempt_ids=[aid], parent_attempt_id="att-fixture-hook-parent", route_id="rt-fixture-notice",
        route_node="execute", receipt=receipt, receipt_digest=pending._canonical_receipt_digest(receipt),
        row_revisions={aid: "fixture-record-revision"})
    environment = {**os.environ, "AGENT_SESSION_ROLE": "worker", "AGENT_DISPATCH_DEPTH": "2"}
    rounds = []
    contexts = []
    for _ in range(2):
        started = time.monotonic()
        try:
            run = subprocess.run([sys.executable, str(source / "hooks/dispatch-session-sweep.py")],
                input=json.dumps({"session_id": session, "hook_event_name": "UserPromptSubmit"}),
                capture_output=True, text=True, env=environment, timeout=10)
            rounds.append({"elapsed_seconds": time.monotonic()-started, "returncode": run.returncode,
                           "notice_emitted": delivery_id in run.stdout, "timed_out": False,
                           "stdout": run.stdout, "stderr_tail": run.stderr[-500:]})
            contexts.append(json.loads(run.stdout)["hookSpecificOutput"]["additionalContext"]
                            if run.stdout else "")
        except subprocess.TimeoutExpired as exc:
            rounds.append({"elapsed_seconds": time.monotonic()-started, "returncode": None,
                           "notice_emitted": False, "timed_out": True,
                           "partial_stdout": (exc.stdout or b"").decode("utf-8", "replace"),
                           "partial_stderr": (exc.stderr or b"").decode("utf-8", "replace")})
            contexts.append("")
    record = pending.read(jobs.parent, session, delivery_id)
    marker = receipt_marker(original, session)
    expected_context = sweep.delivery_context([(jobs.parent, [{**record, sweep.STORAGE_KEY: session}])])
    return {"rounds": rounds, "state": record["state"], "claim_attempts": record["attempts"],
            "marker_counts": [context.splitlines().count(marker) for context in contexts],
            "context_matches_original_record": contexts == [expected_context, ""],
            "original_receipt_preserved": record["receipt"] == original["receipt"],
            "original_identity_preserved": pending.received_ack_identity(record, session, delivery_id)
                == pending.received_ack_identity(original, session, delivery_id),
            "worker_carrier_without_projection": True, "delivery_id": delivery_id}


def _ensure_callback(source: Path):
    start = time.monotonic()
    try:
        run = subprocess.run([sys.executable, str(source / "utilities/peer-steward.py"),
            "__ensure-obligations", "--lifecycle-root", str(source)], timeout=10,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"elapsed_seconds": time.monotonic()-start, "returncode": run.returncode, "timed_out": False}
    except subprocess.TimeoutExpired:
        return {"elapsed_seconds": time.monotonic()-start, "returncode": None, "timed_out": True}


def _row_text(index: int, jobs_path: Path, project_root: Path,
              worker_identities: list[dict[str, str]]):
    if index < OBSERVER_COUNT:
        attempt_id = f"att-observer-{index:02d}"
        parent_id = f"att-parent-{index:02d}"
        status = "running"
    else:
        attempt_id = f"att-history-{index:08d}"
        parent_id = "att-history-parent"
        status = "done"
    fields = [
        "2026-10-18T00:00:00Z", status, str(project_root), str(project_root),
        f"history-{index:08d}",
        "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
        "execution_surface=registered-headless,registered_worker=1,"
        "fallback_hop=same-harness-headless,"
        f"attempt_id={attempt_id},parent_attempt_id={parent_id},harness=codex,",
    ]
    if index < OBSERVER_COUNT:
        fields[-1] += "parent_sid=sid-fixture,parent_harness=claude,"
        fields[-1] += ",".join(f"{key}={value}" for key, value in worker_identities[index].items()) + ","
    fields[-1] += "note="
    prefix = "\t".join(fields)
    desired = 400
    pad = max(0, desired - len(prefix.encode("utf-8")) - 1)
    line = prefix + ("x" * pad) + "\n"
    return line


def _write_jobs(jobs_path: Path, project_root: Path,
                worker_identities: list[dict[str, str]]):
    jobs_path.parent.mkdir(parents=True, exist_ok=True)
    rows = (JOBS_BYTES // 400) + 1024
    with jobs_path.open("w", encoding="utf-8", newline="") as handle:
        for index in range(rows):
            handle.write(_row_text(index, jobs_path, project_root, worker_identities))
    while jobs_path.stat().st_size < JOBS_BYTES:
        with jobs_path.open("a", encoding="utf-8", newline="") as handle:
            for index in range(rows, rows + 1024):
                handle.write(_row_text(index, jobs_path, project_root, worker_identities))
        rows += 1024


def _wait_for(predicate, timeout: float, description: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {description}")


def _active_observer_processes(jobs_path: Path):
    active = {}
    expected_jobs = str(jobs_path)
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().decode(
                errors="replace").split("\0")
        except OSError:
            continue
        if ("--resume-obligation" not in argv or "--jobs" not in argv
                or "codex-managed-completion.py" not in " ".join(argv)):
            continue
        jobs_index = argv.index("--jobs") + 1
        duty_index = argv.index("--resume-obligation") + 1
        if jobs_index >= len(argv) or duty_index >= len(argv) or argv[jobs_index] != expected_jobs:
            continue
        stat_row = _proc_stat(pid)
        if stat_row is not None:
            active.setdefault(argv[duty_index], []).append(
                {"pid": pid, "pid_start": stat_row["start"]})
    return active


def _active_runner_processes(state_root: Path, source: Path):
    active = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            argv = Path(f"/proc/{name}/cmdline").read_bytes().decode(errors="replace").split("\0")
            if (len(argv) < 3 or "__obligation-runner" not in argv or "--state-root" not in argv
                    or Path(argv[1]).resolve() != source / "utilities/peer-steward.py"
                    or Path(argv[argv.index("--state-root")+1]).resolve() != state_root.resolve()):
                continue
            record = _proc_stat(int(name))
            if record:
                active.append({"pid": int(name), "pid_start": record["start"]})
        except (OSError, ValueError, IndexError):
            continue
    return active


class RuntimeObservationCpuFixture(unittest.TestCase):
    def test_cpu_accounting_includes_helpers_reaped_between_proc_samples(self):
        def helper():
            time.sleep(.1)
            subprocess.run([sys.executable, "-c",
                "import time; stop=time.process_time()+.08\nwhile time.process_time()<stop: pass"], check=True)
        thread = threading.Thread(target=helper)
        thread.start()
        try:
            # No /proc sample sees this helper; its real waited CPU still
            # belongs in the process-tree total, exactly once.
            with mock.patch.dict(_sample_window.__globals__, {"_process_tree_snapshot": lambda _seeds: {}}):
                measured = _sample_window(.6, set())
            self.assertEqual(measured["sampled_own_cpu_ticks"], 0)
            self.assertGreaterEqual(measured["cpu_ticks"], .06 * os.sysconf("SC_CLK_TCK"))
        finally:
            thread.join(timeout=3)

    def test_isolated_production_observer_workload(self):
        previous_term = signal.getsignal(signal.SIGTERM)
        def interrupted(_signal, _frame):
            raise KeyboardInterrupt("bounded runtime fixture interrupted")
        signal.signal(signal.SIGTERM, interrupted)
        self.addCleanup(signal.signal, signal.SIGTERM, previous_term)
        with tempfile.TemporaryDirectory(prefix="hearting-runtime-observation-cpu-") as temporary:
            temp_root = Path(temporary)
            data_root = temp_root / "harness-data"
            releases = data_root / "releases"
            releases.mkdir(parents=True)
            source = releases / "fixture"
            _make_source_copy(source)
            current = data_root / "current"
            current.symlink_to(source, target_is_directory=True)
            state_root = temp_root / "peer-state"
            home = temp_root / "home"
            fake_bin = temp_root / "bin"
            profile_dir = temp_root / "profiles"
            profile_hook = temp_root / "profile-hook"
            fake_bin.mkdir()
            fake_herdr = fake_bin / "herdr"
            fake_herdr.write_text(_FAKE_HERDR, encoding="utf-8")
            fake_herdr.chmod(0o755)
            fifo = temp_root / "herdr-wait.fifo"
            os.mkfifo(fifo)
            fifo_keeper = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
            call_log = temp_root / "herdr-calls.log"
            jobs_path = temp_root / "dispatch" / "jobs.log"
            home.mkdir()
            for path in (home / ".config", home / ".local" / "share",
                         home / ".local" / "state", home / ".cache"):
                path.mkdir(parents=True, exist_ok=True)

            old_env = dict(os.environ)
            source_commit = old_env.get("HEARTING_SOURCE_COMMIT", "")
            source_diff_digest = old_env.get("HEARTING_SOURCE_DIFF_SHA256", "")
            if not source_commit:
                try:
                    source_commit = subprocess.check_output(
                        ["git", "rev-parse", "HEAD"], cwd=SOURCE_ROOT,
                        env=old_env, stderr=subprocess.DEVNULL, text=True).strip()
                except (OSError, subprocess.CalledProcessError):
                    source_commit = "source-export"
            if not source_diff_digest:
                try:
                    source_diff = subprocess.check_output(
                        ["git", "diff", "--binary", "HEAD"], cwd=SOURCE_ROOT,
                        env=old_env, stderr=subprocess.DEVNULL)
                    source_diff_digest = hashlib.sha256(source_diff).hexdigest()
                except (OSError, subprocess.CalledProcessError):
                    source_diff_digest = "unavailable"
            isolated = {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_DATA_HOME": str(home / ".local" / "share"),
                "XDG_STATE_HOME": str(home / ".local" / "state"),
                "XDG_CACHE_HOME": str(home / ".cache"),
                "HARNESS_DATA_ROOT": str(data_root),
                "HEARTING_GATES": "on",
                "AGENT_HOME": str(source),
                "AGENT_DISPATCH_JOBS": str(jobs_path),
                "AGENT_PEER_LEDGER_ROOT": str(state_root),
                "HERDR_SOCKET_PATH": str(temp_root / "herdr-observation.sock"),
                "AGENT_HERDR_SESSION": "default",
                "FIXTURE_HERDR_FIFO": str(fifo),
                "FIXTURE_HERDR_LOG": str(call_log),
                "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
                "PYTHONPATH": str(source / "utilities") + os.pathsep
                              + str(source / "tools"),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            diagnostic_dir = temp_root / "runner-diagnostics"
            diagnostic_dir.mkdir()
            profile_hook.mkdir()
            (profile_hook / "sitecustomize.py").write_text(_RUNNER_DIAGNOSTICS)
            isolated["FIXTURE_RUNNER_DIAGNOSTICS"] = str(diagnostic_dir)
            isolated["PYTHONPATH"] = str(profile_hook) + os.pathsep + isolated["PYTHONPATH"]
            if DIAGNOSTIC_PROFILE:
                profile_dir.mkdir()
                (profile_hook / "sitecustomize.py").write_text(
                    _RUNNER_DIAGNOSTICS + "import atexit, cProfile, os, signal, sys\n"
                    "profile_target = ('codex-managed-completion.py' in sys.argv[0] or "
                    "'dispatch-registry.py' in sys.argv[0] or "
                    "'foreground-waiter.py' in sys.argv[0] or "
                    "('peer-steward.py' in sys.argv[0] and "
                    "('__watch-run' in sys.argv or '__obligation-runner' in sys.argv or '__ensure-obligations' in sys.argv)))\n"
                    "if profile_target:\n"
                    "    profiler = cProfile.Profile()\n"
                    "    profiler.enable()\n"
                    "    saved = [False]\n"
                    "    saving = [False]\n"
                    "    def save_profile():\n"
                    "        if saved[0] or saving[0]: return\n"
                    "        saving[0] = True\n"
                    "        profiler.disable()\n"
                    "        path = os.path.join(os.environ['FIXTURE_PROFILE_DIR'], str(os.getpid()) + '.prof')\n"
                    "        profiler.dump_stats(path + '.tmp')\n"
                    "        os.replace(path + '.tmp', path)\n"
                    "        saved[0] = True\n"
                    "        saving[0] = False\n"
                    "    atexit.register(save_profile)\n",
                    encoding="utf-8")
                with (profile_hook / "sitecustomize.py").open("a", encoding="utf-8") as hook:
                    hook.write(
                        "    original_exit = os._exit\n"
                        "    def profiled_exit(code):\n"
                        "        save_profile()\n"
                        "        original_exit(code)\n"
                    "    os._exit = profiled_exit\n"
                    "    def profiled_signal(signum, frame):\n"
                    "        if saving[0]: return\n"
                    "        save_profile()\n"
                    "        raise SystemExit(128 + signum)\n"
                    "    signal.signal(signal.SIGTERM, profiled_signal)\n")
                isolated["FIXTURE_PROFILE_DIR"] = str(profile_dir)
            for key in list(os.environ):
                if (key.startswith(("AGENT_DISPATCH_", "AGENT_PEER_", "HERDR_", "AGENT_HERDR_",
                                    "AGENT_SESSION_", "CLAUDE_", "CODEX_", "OPENCODE_"))
                        or key in {"AGENT_HOME", "HARNESS_DATA_ROOT", "CODEX_HOME",
                                   "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                                   "XDG_STATE_HOME", "XDG_CACHE_HOME"}):
                    os.environ.pop(key, None)
            os.environ.update(isolated)

            runner_process = None
            watch_processes = []
            worker_processes = []
            foreground_processes = []
            foreground_children = set()
            observation_server = None
            control_server = None
            open_successor_locks = contextlib.ExitStack()
            try:
                sys.path.insert(0, str(source / "utilities"))
                sys.path.insert(0, str(source / "tools" / "install"))
                admission = __import__("artifact_admission")
                artifact_index = __import__("artifact_index")
                artifact_identity = __import__("artifact_identity")
                artifact_manifest = __import__("artifact_manifest")
                artifact_producer = __import__("artifact_producer")
                dispatch_contract = __import__("dispatch_contract")
                batch = __import__("dispatch_batch_obligations")
                peer_obligations = __import__("peer_obligations")
                admission_fixture = _import_from(
                    source / "utilities" / "artifact_admission.test.py",
                    "runtime_cpu_admission_fixture")
                producer_fixture = _import_from(
                    source / "utilities" / "artifact_producer.test.py",
                    "runtime_cpu_producer_fixture")
                managed = _import_from(source / "utilities" / "codex-managed-completion.py",
                                       "runtime_cpu_managed_completion")
                steward = _import_from(source / "utilities" / "peer-steward.py",
                                       "runtime_cpu_peer_steward")
                session_tidy = __import__("session_tidy")

                jobs_path.parent.mkdir(parents=True, exist_ok=True)
                worker_identities = []
                for _index in range(OBSERVER_COUNT):
                    worker = subprocess.Popen(
                        [sys.executable, "-c", "import time; time.sleep(600)"],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, start_new_session=True,
                        cwd=str(temp_root), env=dict(os.environ))
                    worker_processes.append(worker)
                    identity = dispatch_contract.process_launch_identity(worker.pid)
                    if not identity.get("pid_start"):
                        raise AssertionError("fixture worker has no exact PID/start identity")
                    worker_identities.append(identity)
                os.environ["FIXTURE_WATCH_SHELL_PID"] = str(os.getpid())
                os.environ["FIXTURE_WATCH_FOREGROUND_PID"] = str(worker_processes[0].pid)
                _write_jobs(jobs_path, source, worker_identities)
                jobs_size = jobs_path.stat().st_size
                if jobs_size < JOBS_BYTES:
                    raise AssertionError(f"jobs registry is only {jobs_size} bytes")

                projects = [_valid_sealed_project(
                    temp_root / "projects" / "project-0", temp_root,
                    {"admission": admission, "identity": artifact_identity,
                     "manifest": artifact_manifest, "artifact_index": artifact_index,
                     "producer": artifact_producer,
                     "producer_fixture": producer_fixture})]
                for index in (1, 2):
                    project = temp_root / "projects" / f"project-{index}"
                    project.mkdir(parents=True)
                    projects.append(_valid_project(
                        project, large_manifest=False, large_index=False,
                        modules={"admission": admission, "identity": artifact_identity,
                                 "manifest": artifact_manifest,
                                 "admission_fixture": admission_fixture}))
                if projects[0]["manifest_bytes"] < MANIFEST_BYTES:
                    raise AssertionError("large manifest threshold was not met")
                if projects[0]["index_bytes"] < INDEX_BYTES:
                    raise AssertionError("large admission index threshold was not met")

                historical_metrics = projects[0].get(
                    "historical_verification_metrics_delta", {})
                if (not BASELINE_MODE and
                        (historical_metrics.get("loads") != 2
                         or historical_metrics.get("hits") != 2)):
                    raise AssertionError(
                        f"sealed verifier did not reuse immutable generations: {historical_metrics}")

                history_projects = _settled_owner_projects(
                    temp_root / "history-projects", jobs_path,
                    {"producer_fixture": producer_fixture, "producer": artifact_producer,
                     "admission": admission, "artifact_index": artifact_index})
                if (history_projects[0]["manifest_bytes"] < MANIFEST_BYTES
                        or history_projects[0]["index_bytes"] < INDEX_BYTES):
                    raise AssertionError("large historical owner did not reach the actual producer path")
                foreground_processes = _start_foreground_waiters(
                    temp_root, jobs_path, source, dispatch_contract)
                for _process, _parent, ready, _result, _attempt in foreground_processes:
                    _wait_for(ready.is_file, 20, "foreground parent-bound waiter startup")
                    foreground_children.add(json.loads(ready.read_text())["child_pid"])
                jobs_size = jobs_path.stat().st_size

                panes = [{"pane_id": "w1:p9", "agent": "claude",
                          "agent_session": {"value": "sid-fixture"},
                          "agent_status": "idle", "name": "fixture-target"}]
                observation_server = FakeObservationEndpoint(
                    Path(isolated["HERDR_SOCKET_PATH"]), panes, agents=[{
                        "pane_id": "w1:p9", "agent": "claude",
                        "agent_session": {"value": "sid-fixture"},
                        "agent_status": "idle", "name": "fixture-target"}])
                control_server = FakeControlEndpoint(temp_root / "control.sock")
                binding = peer_obligations.managed_service_binding(source)
                if binding != str(current):
                    raise AssertionError(f"isolated managed binding unavailable: {binding!r}")

                # Keep real session-tidy seat and successor locks in the isolated state root.
                for key in ("fixture-seat-a", "fixture-seat-b",
                            "peer-successor-fixture-a", "peer-successor-fixture-b"):
                    open_successor_locks.enter_context(session_tidy.seat_lock(key))

                watch_root = steward._watch_root()
                watch_root.mkdir(parents=True, exist_ok=True)
                for index in range(WATCHER_COUNT):
                    watch_id = f"{index + 1:016x}"
                    paths = steward._watch_paths(watch_id, watch_root)
                    arm = {
                        "schema_version": steward._WATCH_SCHEMA,
                        "watch_id": watch_id, "target": "fixture-target",
                        "until": ["idle"], "timeout": 300_000,
                        "deadline": steward._watch_deadline(300_000),
                        "server": "default", "armed_ts": steward._utc_now(),
                        "agent": {"harness": "claude", "session_id": "sid-fixture",
                                  "name": "fixture-target", "pane": "w1:p9"},
                        "steward": {"harness": "codex", "session_id": "sid-steward",
                                    "project": str(source)},
                        "observer_protocol": 2, "observer_generation": 1,
                        "rearm_count": 0, "watcher": {},
                    }
                    steward._write_json_atomic(paths.arm, arm)
                    lock_fd = steward._open_lock(paths.lock)
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    argv = [sys.executable, str(source / "utilities" / "peer-steward.py"),
                            *steward._watch_observer_duty_argv(
                                watch_id, arm, rearm_generation=1, lock_fd=lock_fd,
                                service_current=binding)]
                    log_fd = os.open(paths.log, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
                    try:
                        process = subprocess.Popen(
                            argv, stdin=subprocess.DEVNULL, stdout=log_fd,
                            stderr=subprocess.STDOUT, close_fds=True, pass_fds=(lock_fd,),
                            start_new_session=True, cwd=str(watch_root), env=dict(os.environ))
                    finally:
                        os.close(log_fd)
                        os.close(lock_fd)
                    arm["watcher"] = {"pid": process.pid,
                                      "pid_start": steward.process_start_ticks(process.pid) or ""}
                    steward._write_json_atomic(paths.arm, arm)
                    watch_processes.append((watch_id, process))

                # Create eight real exact-attempt completion duties. The managed
                # runner below launches their actual --resume-obligation entrypoints.
                duty_ids = []
                terminal_observers = max(1, OBSERVER_COUNT // 2)
                observer_workloads = []
                for index in range(OBSERVER_COUNT):
                    terminal_owner = index >= OBSERVER_COUNT - terminal_observers
                    history_index = index - (OBSERVER_COUNT - terminal_observers)
                    selected_attempt = (history_projects[history_index]["owner_attempt_id"] if terminal_owner
                                        else f"att-observer-{index:02d}")
                    parent_args = (["--parent-session-id", "fixture-parent", "--thread-id", "fixture-parent"]
                                   if terminal_owner else ["--parent-attempt-id", f"att-parent-{index:02d}"])
                    args = managed.parser().parse_args([
                        "--jobs", str(jobs_path), "--control-socket",
                        str(temp_root / "control.sock"), *parent_args, "--sealed-batch-id",
                        f"sealed-fixture-{index:02d}", "--attempt-id",
                        selected_attempt, "--interval", "1",
                        "--timeout", "600",
                    ])
                    if terminal_owner:
                        # A session parent with no live TUI binding ends its
                        # observer; bind these to this live fixture process so
                        # the busy-recipient workload stays resident.
                        args.parent_lifetime = {
                            "pid": os.getpid(), "pid_start": _proc_stat(os.getpid())["start"],
                            "session_id": "fixture-parent",
                            "pid_namespace": dispatch_contract.process_namespace_identity()}
                    duty_id, _record = batch.create(args)
                    duty_ids.append(duty_id)
                    observer_workloads.append({"duty_id": duty_id, "attempt_id": selected_attempt,
                        "phase": "terminal-owner-recipient-busy" if terminal_owner else "live-child-join",
                        "parent_lifetime": "session-bound" if terminal_owner else "attempt-bound"})

                if not peer_obligations.ensure_normal_runner(
                        state_root, lifecycle_root=source, reconciliation_required=True):
                    raise AssertionError("managed obligation runner did not start")
                runner_process = peer_obligations._RUNNERS[-1]
                if runner_process.poll() is not None:
                    raise AssertionError("managed obligation runner exited during startup")
                initial_runner = {"pid": runner_process.pid,
                                  "pid_start": _proc_stat(runner_process.pid)["start"]}

                def observer_snapshot():
                    records = [batch.read(jobs_path, duty_id) for duty_id in duty_ids]
                    if any(not record for record in records):
                        return None
                    owners = [record.get("observer") or {} for record in records]
                    if any(not owner.get("pid") or not owner.get("pid_start") for owner in owners):
                        return None
                    for duty_id, owner in zip(duty_ids, owners):
                        pid = int(owner["pid"])
                        proc = _proc_stat(pid)
                        if proc is None or proc["start"] != owner["pid_start"]:
                            return None
                        argv = Path(f"/proc/{pid}/cmdline").read_bytes().decode(
                            errors="replace").split("\0")
                        if "--resume-obligation" not in argv:
                            return None
                        fd_number = argv[argv.index("--obligation-lock-fd") + 1]
                        lease = batch._root(jobs_path) / (duty_id + ".lease")
                        lock_held = peer_obligations._process_holds_flock(
                            pid, lease, fd_number)
                        if not lock_held and not BASELINE_MODE:
                            return None
                        owner["lease_held"] = lock_held
                    return owners

                owners = _wait_for(observer_snapshot, args.launch_ready_timeout, "eight exact managed observers")
                terminal_attempts = {item["attempt_id"] for item in observer_workloads
                                     if item["phase"] == "terminal-owner-recipient-busy"}
                _wait_for(lambda: terminal_attempts <= control_server.deliveries,
                          30, "terminal observers to normalize real history and reach the busy recipient")
                try:
                    _wait_for(lambda: len(observation_server.subscriptions) >= WATCHER_COUNT,
                              20, "eleven actual watch subscriptions")
                except AssertionError as exc:
                    diagnostics = []
                    for watch_id, process in watch_processes:
                        paths = steward._watch_paths(watch_id, watch_root)
                        diagnostics.append({
                            "watch_id": watch_id, "pid": process.pid,
                            "returncode": process.poll(),
                            "observation": (json.loads(paths.observation.read_text())
                                            if paths.observation.exists() else None),
                            "receipt": (json.loads(paths.receipt.read_text())
                                        if paths.receipt.exists() else None),
                            "log": paths.log.read_text(errors="replace")[-2000:]
                                    if paths.log.exists() else "",
                        })
                    raise AssertionError(f"{exc}; watcher diagnostics={diagnostics}") from exc
                watcher_pids = {process.pid for _watch_id, process in watch_processes}
                def pending_watch_readiness():
                    observations = []
                    for watch_id, process in watch_processes:
                        path = steward._watch_paths(watch_id, watch_root).observation
                        if process.poll() is not None or not path.is_file():
                            return None
                        observation = json.loads(path.read_text())
                        if (observation.get("state") != "pending"
                                or observation.get("reason") != "bound-registered-work-pending"
                                or (observation.get("native_wait") or {}).get("agent_state") != "idle"):
                            return None
                        observations.append(observation)
                    return observations
                try:
                    watch_readiness = _wait_for(pending_watch_readiness, 60,
                        "all watchers pending on actual registered work after native idle")
                except AssertionError as exc:
                    diagnostics = []
                    for watch_id, process in watch_processes:
                        paths = steward._watch_paths(watch_id, watch_root)
                        try:
                            observation = (json.loads(paths.observation.read_text())
                                           if paths.observation.exists() else None)
                        except (OSError, ValueError) as error:
                            observation = {"read_error": f"{type(error).__name__}: {error}"}
                        diagnostics.append({
                            "watch_id": watch_id,
                            "target": (steward._read_json(paths.arm) or {}).get("target"),
                            "returncode": process.poll(),
                            "state": observation.get("state") if isinstance(observation, dict) else None,
                            "reason": observation.get("reason") if isinstance(observation, dict) else None,
                            "native_wait": observation.get("native_wait")
                                if isinstance(observation, dict) else None,
                            "receipt_exists": paths.receipt.exists(),
                            "log_tail": paths.log.read_text(errors="replace")[-1200:]
                                if paths.log.exists() else "",
                        })
                    raise AssertionError(
                        f"{exc}; current watcher readiness diagnostics={diagnostics}") from exc
                runner_pids = {runner_process.pid}
                owner_pids = {int(owner["pid"]) for owner in owners}
                worker_pids = {process.pid for process in worker_processes}
                foreground_pids = {process.pid for process, _p, _r, _o, _a in foreground_processes}
                foreground_pids.update(parent.pid for _p, parent, _r, _o, _a in foreground_processes)
                foreground_pids.update(foreground_children)
                seed_pids = watcher_pids | runner_pids | owner_pids | worker_pids | foreground_pids

                runner_counts = []
                def current_seed_pids():
                    current = set(watcher_pids | runner_pids | worker_pids | foreground_pids)
                    active_runners = _active_runner_processes(state_root, source)
                    runner_counts.append(len(active_runners))
                    current.update(record["pid"] for record in active_runners)
                    current.update(int(owner["pid"])
                                   for group in _active_observer_processes(jobs_path).values()
                                   for owner in group)
                    return current

                cold_start = _sample_window(COLD_SECONDS, seed_pids,
                                            seed_provider=current_seed_pids)
                if observer_snapshot() is None:
                    raise AssertionError("observer PID/start or lease changed during cold start")
                warm = _sample_window(IDLE_SECONDS, seed_pids,
                                      seed_provider=current_seed_pids)
                stable_owners = observer_snapshot()
                if stable_owners is None or (not BASELINE_MODE and stable_owners != owners):
                    raise AssertionError("observer PID/start or lease changed during warm idle")
                warm_active = _active_observer_processes(jobs_path)
                for _ in range(3):
                    before = [dict(owner) for owner in stable_owners]
                    batch.ensure_observers(jobs_path, service_current=binding)
                    after = observer_snapshot()
                    if not BASELINE_MODE and after != before:
                        raise AssertionError("an ensure pass duplicated or replaced a healthy observer")
                stop_events = threading.Event()
                sent_events = []
                def publish_events():
                    while not stop_events.is_set():
                        observation_server.publish_unrelated()
                        sent_events.append(WATCHER_COUNT)
                        stop_events.wait(1)
                publisher = threading.Thread(target=publish_events, daemon=True)
                publisher.start()
                try:
                    burst = _sample_window(BURST_SECONDS, seed_pids,
                                           seed_provider=current_seed_pids)
                finally:
                    stop_events.set()
                    publisher.join(timeout=2)
                after_burst = observer_snapshot()
                if not BASELINE_MODE and after_burst != stable_owners:
                    raise AssertionError("unrelated event changed managed observer ownership")
                burst_active = _active_observer_processes(jobs_path)

                # Hook storms use the same installed entry as native session
                # projections. CPU includes short/reaped helpers as well as
                # the persistent runner and its descendants.
                with ThreadPoolExecutor(max_workers=9) as callbacks:
                    ensure_futures = [callbacks.submit(_ensure_callback, source) for _ in range(8)]
                    hook_future = callbacks.submit(_hook_delivery_roundtrip, source, jobs_path)
                    callback_window = _sample_window(20, seed_pids, seed_provider=current_seed_pids)
                    ensure_results = [future.result(timeout=15) for future in ensure_futures]
                    hook_result = hook_future.result(timeout=15)
                if not BASELINE_MODE:
                    if runner_process.poll() is not None:
                        raise AssertionError("activation callbacks replaced an owner with accepted external duties")
                    if any(item["timed_out"] or item["returncode"] != 0 for item in ensure_results):
                        raise AssertionError(f"activation helper did not return promptly: {ensure_results}")
                    if ([item["notice_emitted"] for item in hook_result["rounds"]] != [True, False]
                            or any(item["timed_out"] or item["returncode"] != 0
                                   for item in hook_result["rounds"])
                            or hook_result["state"] != "acked" or hook_result["claim_attempts"] != 1
                            or hook_result["marker_counts"] != [1, 0]
                            or not hook_result["context_matches_original_record"]
                            or not hook_result["original_receipt_preserved"]
                            or not hook_result["original_identity_preserved"]):
                        raise AssertionError(f"worker hook failed output/ack exactly-once fixture: {hook_result}")

                # Exercise real completed-retire successor locks, attached to
                # the records that this runner actually repairs.
                retired = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(.05)"],
                    start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                retired_start = dispatch_contract.process_start_ticks(retired.pid)
                retired.wait(timeout=3)
                successor_locks = contextlib.ExitStack()
                retire_duties = []
                store = peer_obligations.ObligationStore()
                try:
                    for index in range(2):
                        pane = f"w1:pSuccessor{index}"
                        predecessor = f"w1:pRetired{index}"
                        steward._mark_seat_successor(pane, predecessor, "codex", "fixture-successor")
                        duty = store.create(f"retire-lock-fixture-{index}", "retire",
                            {"harness": "claude", "session_id": f"fixture-retired-{index}",
                             "pane": predecessor, "server": "default", "name": "fixture-retired",
                             "foreground": {"pid": retired.pid, "start": retired_start,
                                "shell_pid": os.getpid(), "shell_start": _proc_stat(os.getpid())["start"]}},
                            {"target": predecessor, "requester": {"harness": "codex",
                             "session_id": "fixture-successor", "pane": pane, "server": "default"}})
                        duty = store.update(duty["id"], state="complete", result="normal-exit",
                                            observation={"phase": "complete"})
                        retire_duties.append(duty)
                        successor_locks.enter_context(steward._seat_successor_lock(pane))
                    retry_start = time.monotonic()
                    if not BASELINE_MODE:
                        for duty in retire_duties:
                            steward._resume_retire_obligation(duty, store)
                    retire_retry_seconds = time.monotonic() - retry_start
                    successor_wait_window = _sample_window(
                        20, seed_pids, seed_provider=current_seed_pids)
                    probe = temp_root / "registration-probe.py"
                    probe.write_text(_REGISTRATION_PROBE)
                    started = time.monotonic()
                    registration_run = subprocess.run([sys.executable, str(probe), str(jobs_path),
                        foreground_processes[0][4], history_projects[0]["owner_attempt_id"]],
                        capture_output=True, text=True, timeout=12)
                    registration = json.loads(registration_run.stdout)
                    registration["process_seconds"] = time.monotonic() - started
                    registration["returncode"] = registration_run.returncode
                    if not BASELINE_MODE and (not registration["registered"]
                            or registration["nested_lock_request"] or registration["elapsed_seconds"] >= 4):
                        raise AssertionError(f"mixed workload blocked registration: {registration}")
                finally:
                    successor_locks.close()

                # Check the exact parent-loss stop using the same live wait
                # callback after the CPU windows, without changing sleep.
                process, parent, ready, result_path, _attempt = foreground_processes[0]
                parent.terminate()
                parent.wait(timeout=3)
                process.wait(timeout=6)
                parent_loss = json.loads(result_path.read_text())
                if (parent_loss["failure"] != "parent-terminated" or not parent_loss["group_empty"]):
                    raise AssertionError(f"foreground parent death did not stop its child: {parent_loss}")

                if DIAGNOSTIC_PROFILE:
                    for _watch_id, process in watch_processes:
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGTERM)
                    _wait_for(lambda: all(process.poll() is not None
                                          for _watch_id, process in watch_processes),
                              5, "profiled watcher shutdown")

                # Replacing one observer row is an ownership loss. That process
                # must exit without restoring itself; the managed runner then
                # retries the same duty with one current owner.
                replacement_result = {"mode": "baseline-not-asserted"} if BASELINE_MODE else None
                if not BASELINE_MODE:
                    first_duty = duty_ids[0]
                    first_owner = stable_owners[0]
                    replacement = {"pid": os.getpid(),
                                   "pid_start": str(_proc_stat(os.getpid())["start"])}
                    batch.update(jobs_path, first_duty, state="unknown", observer=replacement,
                                 reason="fixture-observer-record-replaced")
                    _wait_for(lambda: _proc_stat(int(first_owner["pid"])) is None,
                              12, "lost observer to exit")

                    def retry_owner():
                        record = batch.read(jobs_path, first_duty)
                        owner = (record or {}).get("observer") or {}
                        proc = _proc_stat(int(owner["pid"])) if str(owner.get("pid", "")).isdigit() else None
                        if (proc is None or owner.get("pid_start") != proc["start"]
                                or int(owner.get("pid", 0)) == os.getpid()):
                            return None
                        argv = Path(f"/proc/{owner['pid']}/cmdline").read_bytes().decode(
                            errors="replace").split("\0")
                        return owner if "--resume-obligation" in argv else None

                    replacement_owner = _wait_for(retry_owner, 15, "automatic same-duty observer retry")
                    for _ in range(2):
                        batch.ensure_observers(jobs_path, service_current=binding)
                    if retry_owner() != replacement_owner:
                        raise AssertionError("repeated ensure pass replaced the healthy retry owner")
                    replacement_result = {
                        "lost_pid": int(first_owner["pid"]),
                        "new_owner": replacement_owner,
                        "unknown_state_preserved": batch.read(jobs_path, first_duty)["state"] == "unknown",
                    }

                profile_summaries = []
                if DIAGNOSTIC_PROFILE and profile_dir.is_dir():
                    for profile_path in sorted(profile_dir.glob("*.prof")):
                        stats = pstats.Stats(str(profile_path))
                        top = sorted(stats.stats.items(), key=lambda item: item[1][3], reverse=True)[:30]
                        profile_summaries.append({
                            "pid": int(profile_path.stem),
                            "functions_by_cumulative_time": [
                                {"file": key[0], "line": key[1], "function": key[2],
                                 "calls": value[1], "self_seconds": value[2],
                                 "cumulative_seconds": value[3]}
                                for key, value in top],
                        })

                output = {
                    "source_root": str(SOURCE_ROOT),
                    "fixture_root": str(temp_root),
                    "jobs_path": str(jobs_path),
                    "harness_pid": os.getpid(),
                    "source_commit": source_commit,
                    "source_diff_sha256": source_diff_digest,
                    "baseline_mode": BASELINE_MODE,
                    "jobs_bytes": jobs_size,
                    "projects": projects,
                    "settled_owner_projects": history_projects,
                    "observer_workloads": observer_workloads,
                    "busy_recipient_attempts": sorted(control_server.deliveries),
                    "busy_recipient_requests": control_server.requests,
                    "foreground_waiters": 4,
                    "foreground_parent_loss": parent_loss,
                    "callback_storm": callback_window,
                    "ensure_callbacks": ensure_results,
                    "worker_hook_delivery": hook_result,
                    "mixed_registration": registration,
                    "completed_retire_successor_locks": 2,
                    "completed_retire_retry_seconds": retire_retry_seconds,
                    "successor_lock_wait": successor_wait_window,
                    "observers": OBSERVER_COUNT, "watchers": WATCHER_COUNT,
                    "watch_readiness_before_windows": [
                        {"state": item["state"], "reason": item["reason"],
                         "native_state": item["native_wait"]["agent_state"]}
                        for item in watch_readiness],
                    "managed_runner": RUNNER_COUNT,
                    "observer_pid_start": [
                        {"pid": int(item["pid"]), "pid_start": item["pid_start"],
                         "lease_held": bool(item.get("lease_held"))}
                        for item in stable_owners],
                    "warm_active_observer_processes": warm_active,
                    "burst_active_observer_processes": burst_active,
                    "watcher_pid_start": [
                        {"pid": proc.pid,
                         "pid_start": (steward._read_json(
                             steward._watch_paths(watch_id, watch_root).arm) or {}
                             ).get("watcher", {}).get("pid_start", "")}
                        for watch_id, proc in watch_processes],
                    "runner_pid_start": {"pid": runner_process.pid,
                                         "pid_start": initial_runner["pid_start"],
                                         "alive_at_end": runner_process.poll() is None},
                    "current_runner_pid_start": _active_runner_processes(state_root, source),
                    "max_active_runners": max(runner_counts, default=0),
                    "worker_pid_start": [
                        {"pid": process.pid, "pid_start": _proc_stat(process.pid)["start"]}
                        for process in worker_processes],
                    "diagnostic_profile": DIAGNOSTIC_PROFILE,
                    "warm_idle_seconds": IDLE_SECONDS,
                    "unrelated_event_seconds": BURST_SECONDS,
                    "cold_start_seconds": COLD_SECONDS,
                    "cold_start": cold_start,
                    "cold_start_boundary": "post-ready settling window; historical normalization was required before this window",
                    "warm_idle": warm,
                    "unrelated_event": burst,
                    "profile_summaries": profile_summaries,
                    "herdr_requests": dict(observation_server.requests),
                    "unrelated_events_sent": sum(sent_events),
                    "historical_index_projection_metrics_delta": historical_metrics,
                    "observer_replacement_retry": replacement_result,
                    "limitations": [
                        "target-file syscall bytes and per-function hot-branch profiles require a separate strace/profile run",
                        "rchar attribution for sub-50ms exited helpers uses the separate syscall run; CPU includes reaped children",
                    ],
                }
                print(json.dumps(output, indent=2, sort_keys=True))
                evidence_path = os.environ.get("HEARTING_RUNTIME_OBSERVATION_EVIDENCE")
                if evidence_path:
                    path = Path(evidence_path)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n",
                                    encoding="utf-8")
            except BaseException as exc:
                evidence_path = os.environ.get("HEARTING_RUNTIME_OBSERVATION_EVIDENCE")
                if evidence_path:
                    partial = {"fixture_state": "failed", "error": str(exc),
                               "source_commit": source_commit, "source_diff_sha256": source_diff_digest}
                    if runner_process is not None:
                        partial["runner_process"] = {"pid": runner_process.pid,
                                                     "exit_code": runner_process.poll()}
                        for name in ("wchan", "status"):
                            with contextlib.suppress(OSError):
                                partial["runner_process"][name] = Path(f"/proc/{runner_process.pid}/{name}").read_text()
                        ready = diagnostic_dir / f"{runner_process.pid}.ready"
                        log = diagnostic_dir / f"{runner_process.pid}.log"
                        if runner_process.poll() is None and ready.is_file():
                            with contextlib.suppress(ProcessLookupError):
                                os.kill(runner_process.pid, signal.SIGUSR1)
                            time.sleep(.1)  # diagnostic flush after failure, outside CPU windows
                        if log.is_file():
                            partial["runner_process"]["stack_or_exception"] = log.read_text(errors="replace")[-12000:]
                    partial["batch_records"] = [batch.read(jobs_path, duty_id) for duty_id in locals().get("duty_ids", [])]
                    values = locals()
                    for name in ("cold_start", "warm", "burst", "callback_window", "ensure_results",
                                 "hook_result", "registration", "successor_wait_window", "parent_loss",
                                 "history_projects", "jobs_size"):
                        if name in values:
                            partial[name] = values[name]
                    Path(evidence_path).write_text(json.dumps(partial, indent=2, sort_keys=True) + "\n")
                raise
            finally:
                # Every process belongs to this temporary fixture. Stop exact
                # process groups while all state roots and sockets still exist.
                groups = set()
                groups.update(foreground_children)
                for process in [runner_process, *(p for _wid, p in watch_processes),
                                *worker_processes,
                                *(p for p, _parent, _r, _o, _a in foreground_processes),
                                *(p for _wait, p, _r, _o, _a in foreground_processes)]:
                    if process is not None:
                        groups.add(process.pid)
                for duty_id in locals().get("duty_ids", []):
                    record = batch.read(jobs_path, duty_id) if "batch" in locals() else None
                    observer = (record or {}).get("observer") or {}
                    if str(observer.get("pid", "")).isdigit():
                        groups.add(int(observer["pid"]))
                temp_prefix = str(temp_root) + os.sep
                def fixture_process_groups():
                    found = set()
                    for name in os.listdir("/proc"):
                        if not name.isdigit():
                            continue
                        pid = int(name)
                        try:
                            argv = Path(f"/proc/{pid}/cmdline").read_bytes().decode(
                                errors="replace").split("\0")
                        except OSError:
                            continue
                        command = " ".join(argv)
                        if (temp_prefix in command and any(
                                marker in command for marker in (
                                    "codex-managed-completion.py", "peer-steward.py", "/bin/herdr",
                                    "foreground-waiter.py", "foreground-child.py", "registration-probe.py"))):
                            identity = _proc_stat(pid)
                            if identity is not None:
                                found.add(identity["pgrp"])
                    return found

                groups.update(fixture_process_groups())
                for pgid in groups:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(pgid, signal.SIGTERM)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    discovered = fixture_process_groups()
                    if not discovered:
                        break
                    for pgid in discovered:
                        with contextlib.suppress(ProcessLookupError, PermissionError):
                            os.killpg(pgid, signal.SIGTERM)
                    time.sleep(0.05)
                # An observer can start its one same-duty retry while its old
                # lease holder exits. Re-scan after signaling so a child that
                # appeared during teardown is also stopped before temp_root is
                # removed.
                for _ in range(20):
                    discovered = fixture_process_groups()
                    if not discovered:
                        break
                    for pgid in discovered:
                        with contextlib.suppress(ProcessLookupError, PermissionError):
                            os.killpg(pgid, signal.SIGKILL)
                    time.sleep(0.05)
                for pgid in groups:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(pgid, signal.SIGKILL)
                for process in [runner_process, *(p for _wid, p in watch_processes),
                                *worker_processes,
                                *(p for p, _parent, _r, _o, _a in foreground_processes),
                                *(p for _wait, p, _r, _o, _a in foreground_processes)]:
                    if process is not None:
                        with contextlib.suppress(Exception):
                            process.wait(timeout=2)
                if observation_server is not None:
                    observation_server.close()
                if control_server is not None:
                    control_server.close()
                open_successor_locks.close()
                with contextlib.suppress(OSError):
                    os.close(fifo_keeper)
                os.environ.clear()
                os.environ.update(old_env)
                for entry in (str(source / "utilities"), str(source / "tools" / "install")):
                    with contextlib.suppress(ValueError):
                        sys.path.remove(entry)

            if DIAGNOSTIC_PROFILE and "output" in locals():
                # Collect after teardown so the real runner/foreground/observer
                # interpreters have flushed their profiles as well.
                summaries = []
                for profile_path in sorted(profile_dir.glob("*.prof")):
                    stats = pstats.Stats(str(profile_path))
                    top = sorted(stats.stats.items(), key=lambda item: item[1][3], reverse=True)[:30]
                    summaries.append({"pid": int(profile_path.stem), "functions_by_cumulative_time": [
                        {"file": key[0], "line": key[1], "function": key[2], "calls": value[1],
                         "self_seconds": value[2], "cumulative_seconds": value[3]} for key, value in top]})
                output["profile_summaries"] = summaries
                if evidence_path:
                    Path(evidence_path).write_text(json.dumps(output, indent=2, sort_keys=True) + "\n")
                    with tarfile.open(Path(evidence_path).with_suffix(".profiles.tar.gz"), "w:gz") as archive:
                        for profile_path in sorted(profile_dir.glob("*.prof")):
                            archive.add(profile_path, arcname=profile_path.name)
                print(json.dumps({"profiles_collected_after_cleanup": len(summaries)}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
