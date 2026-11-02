#!/usr/bin/env python3
"""Paired before/after measurement for the large state-file read paths (D1).

The same fixed fixture (a 9,800-row ``jobs.log`` and one admission root whose
``index.json`` carries recorded/current production row counts) is measured with
two code roots: the pre-change tree and the worktree.  Every operation runs in
its own child process so the numbers describe the cross-process behaviour real
callers see, not an in-process warm cache.

Measured per operation: wall time, bytes read (``/proc/self/io`` ``rchar`` and
``read_bytes`` deltas), peak RSS, and the module counters the read path already
exposes (``load_index``, ``_parse_owned``, ``canonical_bytes``, ``_write_index``,
``_acquire_lock``/``_release_lock``, ``_build_snapshot``, plus ``_METRICS`` on
code that has them).  Every cached answer is compared with a direct computation
in the same process; any difference fails the run.

Usage::

    python3 tools/measure_state_file_before_after.py --make-fixture \
        --before-code-root <base> --fixture-dir <fixture> \
        --scale recorded --reps 3 \
        --json <out>/baseline-pre.json --markdown <out>/baseline-pre.md
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEFAULT_SCALES = ("recorded", "current")
SCALE_ROWS = {
    # stable_ids, event_ids, streams, extra manifest/cycle/route rows
    "recorded": (176_423, 58_711, 58_711, 1_595),
    "current": (539_854, 180_100, 180_100, 1_595),
}
JOBS_ROWS = 9_800
JOBS_TARGET_BYTES = 53 * 1024 * 1024
NATIVE_CANDIDATES = 106
LEGACY_CANDIDATES = 60
OPEN_ROWS = 20
NOW = 1_792_000_000  # fixed evaluation instant (2026-10-13T...Z), stored in fixture.json


# ---------------------------------------------------------------------------
# child process: fixture builder
# ---------------------------------------------------------------------------

_BUILD = r'''
import hashlib, importlib.util, json, os, random, shutil, sys
from datetime import datetime, timezone
from pathlib import Path

CODE_ROOT = Path(sys.argv[1])
FIXTURE = Path(sys.argv[2])
SCALES = sys.argv[3].split(",")
SCALE_ROWS = {
    "recorded": (176_423, 58_711, 58_711, 1_595),
    "current": (539_854, 180_100, 180_100, 1_595),
}
out = {"code_root": str(CODE_ROOT), "now": 1792000000, "scales": {}}

sys.path.insert(0, str(CODE_ROOT / "utilities"))
import artifact_index as INDEX
import artifact_admission as ADM
import artifact_producer as P
import artifact_identity as ID

rng = random.Random(1102)
CAP = FIXTURE / "capacity"
LOGS = CAP / "logs"
ADM_FIX = FIXTURE / "admission"
for path in (CAP, LOGS, ADM_FIX):
    path.mkdir(parents=True, exist_ok=True)

# --- jobs.log fixture -------------------------------------------------------
def _pad(scale):
    chunks = []
    total = 0
    while total < scale:
        chunk = "".join(rng.choice("0123456789abcdef") for _ in range(16))
        chunks.append(chunk)
        total += 16
    return ",".join("p%d=%s" % (i, c) for i, c in enumerate(chunks))

def _stamp(days_ago):
    return datetime.fromtimestamp(1792000000 - days_ago * 86400, timezone.utc).isoformat().replace("+00:00", "Z")

native_logs = []
rows = []
native_indexes, legacy_indexes, open_indexes = [], [], []
row_order = list(range(9800))
rng.shuffle(row_order)
native_positions = set(row_order[:106])
legacy_positions = set(row_order[106:166])
open_positions = set(sorted(row_order[166:186]))
# Keep the open rows inside the last 27% of the file (production shape).
open_positions = {p for p in open_positions if p >= 7252} or set(list(range(9700, 9720)))
native_positions = {p for p in native_positions if p not in open_positions}
legacy_positions = {p for p in legacy_positions if p not in open_positions}
# Guarantee exact counts by moving any collision into a free slot.
def _ensure(wanted, count, blocked):
    free = [i for i in range(9800) if i not in blocked]
    while len(wanted) < count:
        wanted.add(free.pop())
    return wanted
native_positions = _ensure(set(native_positions), 106, legacy_positions | open_positions)
legacy_positions = _ensure(set(legacy_positions), 60, native_positions | open_positions)
assert len(native_positions) == 106 and len(legacy_positions) == 60

for i in range(9800):
    meta = {}
    if i in native_positions:
        n = len(native_logs)
        attempt = "att-%032x" % (i * 7919 + 11)
        log = LOGS / ("job.%s.claude.jsonl" % attempt)
        log.write_text("".join(json.dumps(r) + "\n" for r in [
            {"type": "rate_limit_event", "session_id": "sid-%d" % n,
             "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour",
                                 "resetsAt": 1792000000 + 3600, "isUsingOverage": False}},
            {"type": "result", "session_id": "sid-%d" % n, "is_error": True,
             "api_error_status": 429, "result": "limit"},
        ]))
        native_logs.append(str(log))
        meta.update(status="done", harness="claude", note="dead-usage-limit",
                    failure_class="capacity", attempt_id=attempt, log_file=str(log),
                    model="fixture-model", quota_scope="scope-%d" % n,
                    quota_scope_kind="claude-subscription-v2", reset="2099-01-01T00:00:00Z",
                    pad=_pad(4200))
        native_indexes.append(i)
    elif i in legacy_positions:
        meta.update(status="done", harness=("codex", "opencode", "claude")[i % 3],
                    note="dead-usage-limit", failure_class="capacity",
                    attempt_id="att-legacy-%04d" % i, reset="2099-01-02T00:00:00Z",
                    pad=_pad(4600))
        legacy_indexes.append(i)
    elif i in open_positions:
        meta.update(status="open", harness="claude", attempt_id="att-open-%04d" % i,
                    pad=_pad(5000))
        open_indexes.append(i)
    else:
        meta.update(status="done", harness="codex", note="pass", failure_class="pass",
                    attempt_id="att-plain-%04d" % i, pad=_pad(5000))
    rows.append("\t".join([_stamp(i % 20), meta.get("status", "done"), str(CODE_ROOT),
                           str(CODE_ROOT), "job-%04d" % i,
                           ",".join("%s=%s" % (k, v) for k, v in meta.items() if k != "status")]))
jobs = CAP / "jobs.log"
jobs.write_text("\n".join(rows) + "\n", encoding="utf-8")
out["jobs"] = {"path": "capacity/jobs.log",
               "bytes": jobs.stat().st_size, "rows": len(rows),
               "sha256": hashlib.sha256(jobs.read_bytes()).hexdigest(),
               "native_rows": sorted(native_indexes),
               "legacy_rows": sorted(legacy_indexes),
               "open_rows": sorted(open_indexes)}

# --- admission root ---------------------------------------------------------
home = FIXTURE / "home"
config = FIXTURE / "config"
state = FIXTURE / "state"
agent_home = FIXTURE / "agent-home"
for path in (home, config, state, agent_home / "core"):
    path.mkdir(parents=True, exist_ok=True)
(agent_home / "core" / "CORE.md").write_text("fixture\n", encoding="utf-8")
root = ADM_FIX / "root"
root.mkdir(parents=True, exist_ok=True)
registry_jobs = ADM_FIX / "jobs.log"
registry_jobs.write_text("", encoding="utf-8")
os.environ.update({
    "HEARTING_GATES": "on", "HOME": str(home), "XDG_CONFIG_HOME": str(config),
    "XDG_STATE_HOME": str(state), "AGENT_HOME": str(agent_home),
    "AGENT_DISPATCH_JOBS": str(registry_jobs),
})
os.environ.pop("AGENT_ARTIFACT_ROOT", None)
for key in list(os.environ):
    if key.startswith("AGENT_DISPATCH_") and key != "AGENT_DISPATCH_JOBS":
        os.environ.pop(key)

spec = importlib.util.spec_from_file_location("fixture_producer_base", CODE_ROOT / "utilities" / "artifact_producer.test.py")
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)
REPO_ID = "repo_" + "a" * 32
ROOT_ID = "root_" + "b" * 32
P.activate(root, repository_id=REPO_ID, artifact_root_id=ROOT_ID)

def _prepare(intensity, slug, campaign_key):
    route = M.compile_for("direct", root, campaign_key=campaign_key, slug=slug)
    binding = M.L.admit_runtime_route(root, route)
    return route, Path(binding.route_file)

def _cycle(route, route_file, capability="autopilot-code"):
    result = P.begin(root, route_file=route_file, capability=capability,
                     intensity="direct", campaign_key="fixture-stream")
    target = Path(result["cycle_dir"]) / "artifacts" / "plans" / "cycle" / "plan.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"fixture plan body\n")
    return result

def _close(route, route_file):
    evidence = FIXTURE / ("evidence-%s.txt" % route["route_id"])
    evidence.write_text("terminal evidence\n", encoding="utf-8")
    for node in route["nodes"]:
        if node.get("terminal") is not True:
            continue
        if node.get("dispatch_depth", 0) == 0:
            M.R.write_completion_marker(route, node, node["id"], evidence)
            continue
        metadata = {"attempt_schema_version": 2, "dispatch_depth": node["dispatch_depth"],
                    "transport": "headless", "execution_surface": "registered-headless",
                    "registered_worker": "1", "fallback_hop": "same-harness-headless"}
        M.R.write_completion_marker(route, node, node["id"], evidence,
                                    attempt_id="att-fixture-%s" % node["id"],
                                    attempt_metadata=metadata)
    outcome, _ = M.R.close_route(route, route_file, commit="a" * 40, summary="fixture")
    assert outcome["terminal_gate_proven"], outcome

route_begin, file_begin = _prepare("direct", "fixture-begin", "fixture-stream")
route_open, file_open = _prepare("direct", "fixture-open", "fixture-stream")
route_sealed, file_sealed = _prepare("direct", "fixture-sealed", "fixture-stream")
cycle_open = _cycle(route_open, file_open)
_close(route_open, file_open)  # sealed-order fixture: close first, finalize on demand
cycle_sealed = _cycle(route_sealed, file_sealed)
_close(route_sealed, file_sealed)
sealed = P.finalize(root, cycle_id=cycle_sealed["cycle_id"])
assert sealed["status"] == "sealed", sealed
record_sealed = P.read_cycle_record(root, cycle_sealed["cycle_id"])
small_index = ADM.load_index(root)
out["admission"] = {
    "root": "admission/root",
    "route_begin": file_begin.relative_to(ADM_FIX).as_posix(),
    "route_sealed": file_sealed.relative_to(ADM_FIX).as_posix(),
    "cycle_open": cycle_open["cycle_id"],
    "cycle_sealed": cycle_sealed["cycle_id"],
    "producer_sealed": record_sealed["producer_id"],
    "campaign": cycle_open["campaign_id"],
    "small_index": {"stable": len(small_index.stable_ids),
                    "manifests": len(small_index.manifests),
                    "cycles": len(small_index.cycles),
                    "routes": sum(len(b) for b in small_index.routes.values())},
    "manifests": small_index.manifests,
    "cycles": small_index.cycles,
    "routes": small_index.routes,
    "root_id": small_index.artifact_root_id,
}

# --- bulk index per scale ---------------------------------------------------
for scale in SCALES:
    stable_n, event_n, stream_n, extra_n = SCALE_ROWS[scale]
    stable = {}
    for i in range(stable_n):
        stable["art_%032x" % (i * 2654435761 % (1 << 128))] = {
            "kind": "artifact", "cycle_id": "cyc_%032x" % (i % 997),
            "manifest_id": "man_%032x" % (i % 991)}
    event_ids, streams = {}, {}
    for i in range(event_n):
        stream_id = "strm_%032x" % (i % max(1, stream_n))
        event_id = "evt_%032x" % i
        event_ids[event_id] = {"stream_id": stream_id, "stream_sequence": 1,
                               "cycle_id": "cyc_%032x" % (i % 997)}
        if i < stream_n:
            streams[stream_id] = {"last_sequence": 1, "cycle_id": "cyc_%032x" % (i % 997),
                                  "last_event_id": event_id}
    manifests = dict(small_index.manifests)
    cycles = dict(small_index.cycles)
    routes = {k: dict(v) for k, v in small_index.routes.items()}
    for i in range(extra_n):
        key = "extra-%06d" % i
        manifests[key] = {"manifest_id": "man_x%028x" % i, "manifest_revision_id": "mrev_x%024x" % i,
                          "cycle_id": "cyc_x%028x" % i, "manifest_digest": "sha256:%064x" % i,
                          "idempotency_key": key}
        cycles["cyc_x%028x" % i] = {"campaign_id": "camp_x%028x" % i,
                                    "cycle_path": "extra/%06d" % i,
                                    "manifest_digest": "sha256:%064x" % i}
        routes.setdefault("root_" + "b" * 32, {})["route-x%027x" % i] = {
            "cycle_id": "cyc_x%028x" % i, "route_hash": None}
    big = INDEX.IndexDocument(
        schema_version=small_index.schema_version,
        artifact_root_id=small_index.artifact_root_id,
        stable_ids=stable, routes=routes, event_ids=event_ids, streams=streams,
        manifests=manifests, cycles=cycles)
    ADM._write_index(root, big)
    index_path = ADM._index_path(root)
    out["scales"][scale] = {
        "root": "admission/root", "index_bytes": index_path.stat().st_size,
        "stable_rows": stable_n, "event_rows": event_n, "stream_rows": stream_n,
        "extra_manifest_rows": extra_n,
        "index_sha256": hashlib.sha256(index_path.read_bytes()).hexdigest(),
    }
    (ADM_FIX / ("index-%s.sha256" % scale)).write_text(out["scales"][scale]["index_sha256"] + "\n")
    shutil.copytree(root, ADM_FIX / "roots" / scale)

(FIXTURE / "fixture.json").write_text(json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps({"ok": True, "fixture": str(FIXTURE), "scales": list(SCALES),
                  "jobs_bytes": out["jobs"]["bytes"],
                  "index_bytes": {k: v["index_bytes"] for k, v in out["scales"].items()}}))
'''


# ---------------------------------------------------------------------------
# child process: one measured operation
# ---------------------------------------------------------------------------

_CHILD = r'''
import hashlib, json, os, resource, subprocess, sys, time

CODE_ROOT = os.environ["M_CODE_ROOT"]
SPEC = json.loads(sys.argv[1])
sys.path.insert(0, str(CODE_ROOT + "/utilities"))
os.chdir(CODE_ROOT)
for key in list(os.environ):
    if key.startswith("AGENT_DISPATCH_") and key != "AGENT_DISPATCH_JOBS":
        os.environ.pop(key)
os.environ.setdefault("HEARTING_GATES", "on")

def io_snapshot():
    values = {}
    try:
        for line in open("/proc/self/io"):
            name, _, value = line.partition(":")
            values[name.strip()] = int(value.strip())
    except OSError:
        pass
    return values

def rss_kb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

COUNTERS = {}
def wrap(module, name, label):
    original = getattr(module, name, None)
    if original is None:
        return None
    def counted(*args, **kwargs):
        COUNTERS[label] = COUNTERS.get(label, 0) + 1
        return original(*args, **kwargs)
    setattr(module, name, counted)
    return original

def timed(fn):
    before = io_snapshot()
    start = time.perf_counter()
    value = fn()
    wall = time.perf_counter() - start
    after = io_snapshot()
    return value, {
        "wall_ms": round(wall * 1000, 3),
        "rchar": after.get("rchar", 0) - before.get("rchar", 0),
        "read_bytes": after.get("read_bytes", 0) - before.get("read_bytes", 0),
        "maxrss_kb": rss_kb(),
    }

def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]

op = SPEC["op"]
result = {"op": op}

if op in ("capacity-read", "capacity-write"):
    import dispatch_capacity_evidence as CAP
    jobs = SPEC["jobs"]
    now = SPEC.get("now")
    if op == "capacity-write":
        mode = SPEC["mode"]
        lines = open(jobs, "r", encoding="utf-8", errors="replace").read().splitlines()
        try:
            index = SPEC["line"]
            if mode in ("close", "comment"):
                fields = lines[index].split("\t")
                if mode == "close":
                    fields[1] = "done"
                    fields[5] += ",failure_class=pass,note=pass"
                else:
                    fields[5] = fields[5].replace("note=pass", "note=pass-old", 1)
                lines[index] = "\t".join(fields)
            elif mode == "same-size-rewrite":
                fields = lines[index].split("\t")
                old = fields[5]
                meta = old.split(",")
                for pos, cell in enumerate(meta):
                    if cell.startswith("p0="):
                        body = cell[3:]
                        meta[pos] = "p0=" + ("f" if body[0] != "f" else "0") + body[1:]
                        break
                fields[5] = ",".join(meta)
                new_line = "\t".join(fields)
                assert len(new_line) == len(lines[index]), "same-size rewrite changed length"
                lines[index] = new_line
        except KeyError:
            pass
        append_helper = getattr(__import__("dispatch_contract"), "_append_registry_rows", None)
        if mode == "append":
            row = SPEC["row"]
            if append_helper is not None:
                append_helper(__import__("pathlib").Path(jobs), [row])
            else:
                with open(jobs, "a", encoding="utf-8") as handle:
                    handle.write(row + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
        elif mode == "legacy-replace":
            new_bytes = ("\n".join(lines) + "\n").encode()
            tmp = jobs + ".legacy-new"
            with open(tmp, "wb") as handle:
                handle.write(new_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, jobs)
        elif mode == "same-size-rewrite":
            st = os.stat(jobs)
            with open(jobs, "r+b") as handle:
                handle.seek(0)
                handle.write(("\n".join(lines) + "\n").encode())
                handle.flush()
                os.fsync(handle.fileno())
            os.utime(jobs, ns=(st.st_atime_ns, st.st_mtime_ns))
        else:
            import dispatch_contract as DC
            import pathlib
            DC._atomic_registry_replace(pathlib.Path(jobs), lines)
        result["answer"] = fingerprint(lines[SPEC.get("line", 0)] if "line" in SPEC else len(lines))
    else:
        reads = {"read_text_jobs": 0, "read_bytes_jobs": 0}
        real_read_text, real_read_bytes = __import__("pathlib").Path.read_text, __import__("pathlib").Path.read_bytes
        def read_text(path, *args, **kwargs):
            if str(path) == str(jobs):
                reads["read_text_jobs"] += 1
            return real_read_text(path, *args, **kwargs)
        def read_bytes(path, *args, **kwargs):
            if str(path) == str(jobs):
                reads["read_bytes_jobs"] += 1
            return real_read_bytes(path, *args, **kwargs)
        __import__("pathlib").Path.read_text = read_text
        __import__("pathlib").Path.read_bytes = read_bytes
        wrap(CAP, "_build_snapshot", "build_snapshot_calls")
        snapshot_value, timing = timed(lambda: CAP._snapshot(jobs, now))
        timing["counters"] = dict(COUNTERS)
        timing["counters"].update(reads)
        if hasattr(CAP, "_METRICS"):
            timing["metrics"] = dict(CAP._METRICS)
        def _normalized(snapshot):
            return {
                "native": [{k: v for k, v in row.items() if k not in ("e", "ls", "x")}
                           for row in snapshot["native"]],
                "legacy": [{k: v for k, v in row.items() if k not in ("rr", "x")}
                           for row in snapshot["legacy"]],
            }
        candidates = _normalized(snapshot_value)
        cached_answers = {"usage": CAP._usage(jobs, now=now, env=SPEC["env"]),
                          "observations": CAP.observations(jobs, now=now, env=SPEC["env"]),
                          "limits": CAP.active_limits(jobs, now=now, env=SPEC["env"])}
        CAP._DISK_CACHE = False
        direct_snapshot = CAP._snapshot(jobs, now)
        direct_answers = {"usage": CAP._usage(jobs, now=now, env=SPEC["env"]),
                          "observations": CAP.observations(jobs, now=now, env=SPEC["env"]),
                          "limits": CAP.active_limits(jobs, now=now, env=SPEC["env"])}
        result["timing"] = timing
        result["candidates"] = fingerprint(candidates)
        result["answers"] = fingerprint(cached_answers)
        result["direct_candidates"] = fingerprint(_normalized(direct_snapshot))
        result["direct_answers"] = fingerprint(direct_answers)
        result["correct"] = (result["candidates"] == result["direct_candidates"]
                             and result["answers"] == result["direct_answers"])

elif op == "lru-scan":
    import dispatch_contract as DC
    path = SPEC["jobs"]
    rows = open(path, "r", encoding="utf-8", errors="replace").read().splitlines()
    scans = []
    for _ in range(SPEC.get("passes", 3)):
        before = io_snapshot()
        start = time.perf_counter()
        for line in rows:
            fields = line.split("\t")
            if len(fields) == 6:
                DC.parse_registry_metadata(fields[5])
        scans.append({"wall_ms": round((time.perf_counter() - start) * 1000, 3),
                      "rchar": io_snapshot().get("rchar", 0) - before.get("rchar", 0)})
    info = DC._registry_metadata_items.cache_info()
    result["scans"] = scans
    result["cache_info"] = {"hits": info.hits, "misses": info.misses,
                            "maxsize": info.maxsize, "currsize": info.currsize}
    result["maxrss_kb"] = rss_kb()

else:
    import artifact_admission as ADM
    import artifact_producer as P
    import artifact_index as INDEX
    root = SPEC["root"]
    wrap(ADM, "load_index", "load_index_calls")
    wrap(ADM, "load_index_head", "load_index_head_calls")
    wrap(INDEX, "_parse_owned", "parse_owned_calls")
    wrap(INDEX, "canonical_bytes", "canonical_bytes_calls")
    wrap(ADM, "_write_index", "write_index_calls")
    holds = {}
    real_acquire, real_release = ADM._acquire_lock, ADM._release_lock
    def acquire(root_, timeout, now=None):
        fd = real_acquire(root_, timeout, now)
        holds[fd] = time.perf_counter()
        return fd
    def release(root_, fd):
        COUNTERS["lock_held_ms"] = round(COUNTERS.get("lock_held_ms", 0.0)
                                         + (time.perf_counter() - holds.pop(fd, time.perf_counter())) * 1000, 3)
        return real_release(root_, fd)
    ADM._acquire_lock, ADM._release_lock = acquire, release
    def counters():
        values = dict(COUNTERS)
        for module in (ADM,):
            metrics = getattr(module, "_METRICS", None)
            if isinstance(metrics, dict):
                values.update(metrics)
        return values

    def run(label, fn):
        base_counters = counters()
        value, timing = timed(fn)
        after_counters = counters()
        timing["counters"] = {key: after_counters[key] - base_counters.get(key, 0)
                              for key in after_counters}
        return value, timing

    if op == "admission-prepare":
        value, timing = run("prepare", lambda: ADM.load_index(root))
        result["timing"] = timing
        result["rows"] = [len(value.stable_ids), len(value.manifests), len(value.cycles)]
    elif op == "admission-begin":
        value, timing = run("begin", lambda: P.begin(
            root, route_file=SPEC["route"], capability="autopilot-code",
            intensity="direct", campaign_key="fixture-stream"))
        index = ADM.load_index(root)
        result["timing"] = timing
        result["answer"] = {"status": value.get("status"), "layout": value.get("layout"),
                            "index_rows": [len(index.stable_ids), len(index.manifests), len(index.cycles)]}
    elif op == "admission-seal":
        value, timing = run("seal", lambda: P.finalize(root, cycle_id=SPEC["cycle"]))
        result["timing"] = timing
        result["answer"] = {"status": value.get("status")}
    elif op == "admission-verify":
        record = P.read_cycle_record(root, SPEC["cycle"])
        binding = {"cycle_id": SPEC["cycle"], "producer_id": record["producer_id"]}
        value, timing = run("verify", lambda: P.verify_finalized_cycle(
            root, cycle_id=SPEC["cycle"], expected_binding=binding))
        result["timing"] = timing
        result["answer"] = {"verified": bool(value.get("verified", value))}
    elif op == "admission-retry":
        value, timing = run("retry", lambda: P.finalize(root, cycle_id=SPEC["cycle"]))
        result["timing"] = timing
        result["answer"] = {"status": value.get("status")}
    elif op == "admission-chain":
        sequence = []
        value, timing = run("seal", lambda: P.finalize(root, cycle_id=SPEC["cycle"]))
        sequence.append(("seal", timing, {"status": value.get("status")}))
        time.sleep(SPEC.get("settle", 0.15))
        record = P.read_cycle_record(root, SPEC["cycle"])
        binding = {"cycle_id": SPEC["cycle"], "producer_id": record["producer_id"]}
        value, timing = run("verify", lambda: P.verify_finalized_cycle(
            root, cycle_id=SPEC["cycle"], expected_binding=binding))
        sequence.append(("verify", timing, {"verified": bool(value.get("verified", value))}))
        value, timing = run("retry", lambda: P.finalize(root, cycle_id=SPEC["cycle"]))
        sequence.append(("retry", timing, {"status": value.get("status")}))
        result["sequence"] = [{"step": step, **timing, "answer": answer}
                              for step, timing, answer in sequence]
    elif op == "admission-verify-index":
        value, timing = run("verify_index", lambda: ADM.verify_index(root))
        result["timing"] = timing
        result["answer"] = {"ok": bool(value.ok),
                            "violations": sorted(v.code for v in value.violations),
                            "warnings": sorted(v.code for v in value.warnings)}
    else:
        raise SystemExit("unknown op " + op)

print(json.dumps(result, sort_keys=True))
'''


# ---------------------------------------------------------------------------
# parent helpers
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _host() -> dict:
    info = {"python": sys.version.split()[0], "cpu_count": os.cpu_count()}
    try:
        info["loadavg"] = list(os.getloadavg())
    except OSError:
        pass
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith(("MemTotal:", "MemAvailable:")):
                name, _, value = line.partition(":")
                info[name.strip()] = value.strip()
    except OSError:
        pass
    try:
        st = os.statvfs("/home/nas/user/Uihyeop/personal/hearting/.agent_runtime")
        info["fs_blocks_free"] = st.f_bavail * st.f_frsize
    except OSError:
        pass
    return info


def _run_child(code_root: Path, spec: dict, env: dict, timeout: int = 900) -> dict:
    child_env = dict(os.environ)
    child_env.update(env)
    child_env["M_CODE_ROOT"] = str(code_root)
    child_env["PYTHONPATH"] = str(code_root / "utilities")
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-B", "-c", _CHILD, json.dumps(spec)],
        cwd=str(code_root), env=child_env, capture_output=True, text=True, timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError("child failed (%s): %s\n%s" % (
            spec.get("op"), completed.stdout[-2000:], completed.stderr[-4000:]))
    try:
        return json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise RuntimeError("child produced no result: %s\n%s" % (exc, completed.stderr[-4000:]))


def _copy_tree(source: Path, target: Path) -> Path:
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    return target


def _median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def _summarize(runs: list[dict]) -> dict:
    if not runs:
        return {}
    def pick(path):
        out = []
        for run in runs:
            value = run
            for part in path:
                value = (value or {}).get(part) if isinstance(value, dict) else None
            out.append(value)
        return out
    keys = ("wall_ms", "rchar", "read_bytes", "maxrss_kb", "lock_held_ms",
            "load_index_calls", "parse_owned_calls", "canonical_bytes_calls",
            "write_index_calls", "load_index_head_calls", "build_snapshot_calls",
            "read_text_jobs", "read_bytes_jobs")
    summary = {key: _median([v for v in pick(["timing", key]) if v is not None]) for key in keys}
    counters = {}
    for run in runs:
        for key, value in ((run.get("timing") or {}).get("counters") or {}).items():
            counters.setdefault(key, []).append(value)
    summary["counters"] = {key: _median(values) for key, values in counters.items()}
    if pick(["timing", "metrics"]):
        metrics = {}
        for run in runs:
            for key, value in ((run.get("timing") or {}).get("metrics") or {}).items():
                metrics.setdefault(key, []).append(value)
        summary["metrics"] = {key: _median(values) for key, values in metrics.items()}
    return summary


def _scenario_ok(runs: list[dict]) -> bool:
    for run in runs:
        if run.get("correct") is False:
            return False
        sequence = run.get("sequence") or []
        if any(step.get("answer", {}).get("verified") is False for step in sequence):
            return False
    return True


def _scenario(runs: list[dict]) -> dict:
    return {"reps": len(runs), "ok": _scenario_ok(runs), "median": _summarize(runs),
            "answers": [run.get("answer") for run in runs],
            "raw": runs}


class Fixture:
    def __init__(self, directory: Path):
        self.directory = directory
        self.meta = json.loads((directory / "fixture.json").read_text())
        self.now = self.meta["now"]

    def verify(self) -> None:
        jobs = self.directory / "capacity" / "jobs.log"
        if _sha256(jobs) != self.meta["jobs"]["sha256"]:
            raise RuntimeError("fixture jobs.log digest changed")
        for scale, info in self.meta["scales"].items():
            index = (self.directory / "admission" / "roots" / scale
                     / ".runtime" / "artifact-admission" / "v1" / "index.json")
            if not index.exists() or _sha256(index) != info["index_sha256"]:
                raise RuntimeError("fixture index.json digest changed (%s)" % scale)

    def env(self) -> dict:
        return {"HOME": str(self.directory / "home"),
                "XDG_CONFIG_HOME": str(self.directory / "config"),
                "XDG_STATE_HOME": str(self.directory / "state"),
                "AGENT_HOME": str(self.directory / "agent-home"),
                "AGENT_DISPATCH_JOBS": str(self.directory / "admission" / "jobs.log"),
                "HEARTING_GATES": "on"}


CAPACITY_SCENARIOS = ("C0-cold-build", "C1-warm-hit", "C2-append", "C3-close-recent",
                      "C4-close-earliest", "C5-comment-old", "C6-legacy-replace",
                      "C7-same-size-rewrite")


def _capacity_line_for(mode: str, meta: dict) -> tuple[str, int]:
    if mode == "C3-close-recent":
        return "close", max(meta["jobs"]["open_rows"])
    if mode == "C4-close-earliest":
        return "close", min(meta["jobs"]["open_rows"])
    if mode == "C5-comment-old":
        candidates = [n for n in range(meta["jobs"]["rows"])
                      if n not in set(meta["jobs"]["native_rows"])
                      and n not in set(meta["jobs"]["legacy_rows"])
                      and n not in set(meta["jobs"]["open_rows"])]
        return "comment", candidates[int(len(candidates) * 0.10)]
    if mode == "C7-same-size-rewrite":
        return "same-size-rewrite", meta["jobs"]["legacy_rows"][0]
    return "", 0


def measure_capacity(code_root: Path, fixture: Fixture, scratch: Path, reps: int) -> dict:
    base = fixture.directory / "capacity"
    env = fixture.env()
    scenarios = {}
    for name in CAPACITY_SCENARIOS:
        runs = []
        for rep in range(reps):
            work = _copy_tree(base, scratch / ("cap-%s-%d" % (name, rep)))
            jobs = work / "jobs.log"
            if name == "C1-warm-hit":
                _run_child(code_root, {"op": "capacity-read", "jobs": str(jobs),
                                       "now": fixture.now, "env": env}, env)
            if name != "C0-cold-build" and name != "C1-warm-hit":
                # Operational steady state: a published cache exists before the
                # change, so the measured read is the post-change one.
                _run_child(code_root, {"op": "capacity-read", "jobs": str(jobs),
                                       "now": fixture.now, "env": env}, env)
            if name in ("C2-append", "C3-close-recent", "C4-close-earliest",
                        "C5-comment-old", "C6-legacy-replace", "C7-same-size-rewrite"):
                if name == "C2-append":
                    row = ("2026-10-13T00:00:00Z\tdone\t%s\t%s\tjob-appended\t"
                           "attempt_id=att-appended,harness=codex,note=pass,failure_class=pass,pad=%s"
                           % (code_root, code_root, "a" * 4000))
                    spec = {"op": "capacity-write", "jobs": str(jobs), "mode": "append", "row": row}
                elif name == "C6-legacy-replace":
                    spec = {"op": "capacity-write", "jobs": str(jobs), "mode": "legacy-replace"}
                else:
                    mode, line = _capacity_line_for(name, fixture.meta)
                    spec = {"op": "capacity-write", "jobs": str(jobs), "mode": mode, "line": line}
                _run_child(code_root, spec, env)
                time.sleep(0.15)  # cache keys are only trusted once the file is settled
            run = _run_child(code_root, {"op": "capacity-read", "jobs": str(jobs),
                                         "now": fixture.now, "env": env}, env)
            if name.startswith("C"):
                run["correct"] = bool(run.get("correct"))
            runs.append(run)
            shutil.rmtree(work, ignore_errors=True)
        scenarios[name] = _scenario(runs)
    return scenarios


def measure_lru(code_root: Path, fixture: Fixture, reps: int) -> dict:
    runs = []
    for _ in range(reps):
        run = _run_child(code_root, {"op": "lru-scan", "jobs": str(fixture.directory / "capacity" / "jobs.log"),
                                     "passes": 3}, fixture.env())
        runs.append(run)
    summary = {"reps": len(runs), "ok": True,
               "median": {"scans": [_median([run["scans"][i]["wall_ms"] for run in runs]) for i in range(3)],
                          "misses": [_median([run["cache_info"]["misses"] for run in runs])],
                          "maxrss_kb": _median([run["maxrss_kb"] for run in runs])},
               "raw": runs}
    return summary


ADMISSION_SCENARIOS = ("A1-begin", "A2-seal", "A3-verify", "A4-retry", "A5-chain",
                       "A6-headless-first-verify", "A7-verify-index")


def _restore_root(pristine: Path, root: Path) -> None:
    """Restore the measurement root in place (same absolute path, so route hashes hold).

    A detached checkpoint helper can recreate ``.runtime`` between the delete and
    the copy, so the copy merges into a fresh directory instead of assuming the
    path is absent.
    """
    for _ in range(5):
        shutil.rmtree(root, ignore_errors=True)
        if not root.exists():
            break
        time.sleep(0.05)
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(pristine, root, dirs_exist_ok=True)


def measure_admission(code_root: Path, fixture: Fixture, scratch: Path, reps: int, scale: str) -> dict:
    """Measure one scale in a pristine root restored in place before every rep.

    The route hash and the sealed manifest bind absolute root paths, so the root
    is restored at its fixture path instead of being copied to a new one.
    """
    meta = fixture.meta
    env = fixture.env()
    pristine = fixture.directory / "admission" / "roots" / scale
    root = fixture.directory / "admission" / "root"
    route = str(fixture.directory / "admission" / meta["admission"]["route_begin"])
    scenarios = {}
    for name in ADMISSION_SCENARIOS:
        runs = []
        for rep in range(reps):
            _restore_root(pristine, root)
            prep = _run_child(code_root, {"op": "admission-prepare", "root": str(root)}, env)
            time.sleep(0.15)
            if name == "A6-headless-first-verify":
                head = root / ".runtime" / "artifact-admission" / "v1" / "index.json.head.json"
                if head.exists():
                    head.unlink()
            if name == "A1-begin":
                run = _run_child(code_root, {"op": "admission-begin", "root": str(root),
                                             "route": route}, env)
            elif name == "A2-seal":
                run = _run_child(code_root, {"op": "admission-seal", "root": str(root),
                                             "cycle": meta["admission"]["cycle_open"]}, env)
            elif name == "A3-verify":
                run = _run_child(code_root, {"op": "admission-verify", "root": str(root),
                                             "cycle": meta["admission"]["cycle_sealed"]}, env)
            elif name == "A4-retry":
                run = _run_child(code_root, {"op": "admission-retry", "root": str(root),
                                             "cycle": meta["admission"]["cycle_sealed"]}, env)
            elif name == "A5-chain":
                run = _run_child(code_root, {"op": "admission-chain", "root": str(root),
                                             "cycle": meta["admission"]["cycle_open"],
                                             "settle": 0.15}, env)
            elif name == "A6-headless-first-verify":
                first = _run_child(code_root, {"op": "admission-verify", "root": str(root),
                                               "cycle": meta["admission"]["cycle_sealed"]}, env)
                second = _run_child(code_root, {"op": "admission-verify", "root": str(root),
                                                "cycle": meta["admission"]["cycle_sealed"]}, env)
                run = {"op": name, "first": first, "second": second,
                       "timing": first["timing"],
                       "second_timing": second["timing"],
                       "answer": {"first": first.get("answer"), "second": second.get("answer")}}
            else:
                run = _run_child(code_root, {"op": "admission-verify-index", "root": str(root)}, env)
            run["prepare"] = prep.get("timing")
            runs.append(run)
        scenarios[name] = _scenario([r for r in runs if "first" not in r])
        if name == "A6-headless-first-verify":
            scenarios[name]["raw"] = runs
            scenarios[name]["median"] = _summarize([{**r, "timing": r["first"]["timing"]} for r in runs])
        if name == "A5-chain":
            steps = [step["step"] for step in runs[0]["sequence"]] if runs else []
            scenarios[name]["median"] = {
                step: _summarize([{"timing": next(s for s in run["sequence"] if s["step"] == step)}
                                  for run in runs])
                for step in steps}
    _restore_root(pristine, root)
    return scenarios


def measure(root: Path, fixture: Fixture, scratch: Path, scales, reps: int) -> dict:
    result = {"code_root": str(root), "scales": {}}
    result["capacity"] = measure_capacity(root, fixture, scratch, reps)
    result["lru"] = measure_lru(root, fixture, reps)
    for scale in scales:
        result["scales"][scale] = {"admission": measure_admission(root, fixture, scratch, reps, scale)}
    return result


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

_REPORT_KEYS = (
    ("wall_ms", "wall ms"),
    ("rchar", "rchar bytes"),
    ("read_bytes", "read_bytes"),
    ("maxrss_kb", "RSS KB"),
    ("load_index_calls", "load_index"),
    ("parse_owned_calls", "parse"),
    ("write_index_calls", "write_index"),
    ("canonical_bytes_calls", "canonical"),
    ("lock_held_ms", "lock ms"),
    ("build_snapshot_calls", "build_snapshot"),
    ("read_text_jobs", "read_text(jobs)"),
)


def _markdown(payload: dict) -> str:
    lines = ["# State-file read measurement", "",
             "Fixture: `%s` (jobs.log %s bytes / %s rows; index %s)." % (
                 payload["fixture"]["path"], payload["fixture"]["jobs"]["bytes"],
                 payload["fixture"]["jobs"]["rows"],
                 ", ".join("%s=%s bytes" % (k, v["index_bytes"])
                           for k, v in payload["fixture"]["scales"].items())),
             "", "Host: %s" % json.dumps(payload["host"], sort_keys=True), "",
             "Every operation runs in its own child process on a fresh fixture copy;",
             "the page cache is warm.  Bytes are `/proc/self/io` `rchar` deltas for the",
             "timed call only; counters are the module-level call counters for that call.", ""]

    def table(title, before, after):
        lines.append("## " + title)
        lines.append("")
        lines.append("| scenario | wall ms before | wall ms after | rchar before | rchar after "
                     "| parse before | parse after | load_index b/a | lock ms b/a |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for name in before:
            if name not in after:
                continue
            b = before[name].get("median", {}) or {}
            a = after[name].get("median", {}) or {}
            bc = b.get("counters") or {}
            ac = a.get("counters") or {}
            lines.append("| %s | %s | %s | %s | %s | %s | %s | %s/%s | %s/%s |" % (
                name, _fmt(b.get("wall_ms")), _fmt(a.get("wall_ms")),
                _fmt(b.get("rchar")), _fmt(a.get("rchar")),
                _fmt(bc.get("parse_owned_calls")), _fmt(ac.get("parse_owned_calls")),
                _fmt(b.get("load_index_calls", bc.get("load_index_calls"))),
                _fmt(a.get("load_index_calls", ac.get("load_index_calls"))),
                _fmt(b.get("lock_held_ms")), _fmt(a.get("lock_held_ms"))))
        lines.append("")

    table("capacity", payload["before"]["capacity"], payload["after"]["capacity"])
    for scale in payload["scales"]:
        table("admission (%s)" % scale,
              payload["before"]["scales"][scale]["admission"],
              payload["after"]["scales"][scale]["admission"])
    lines.append("## admission chain (settle -> verify -> retry)")
    lines.append("")
    for scale in payload["scales"]:
        chain = payload["after"]["scales"][scale]["admission"].get("A5-chain", {}).get("median") or {}
        lines.append("- %s: %s" % (scale, ", ".join(
            "%s %sms parse=%s" % (step, _fmt(value.get("wall_ms")),
                                  _fmt((value.get("counters") or {}).get("parse_owned_calls")))
            for step, value in chain.items())))
    lines.append("")
    lines.append("## jobs metadata LRU")
    lines.append("")
    for label in ("before", "after"):
        lru = payload[label]["lru"]["median"]
        lines.append("- %s: scans ms %s, misses %s, RSS %s KB" % (
            label, lru["scans"], lru["misses"], _fmt(lru["maxrss_kb"])))
    lines.append("")
    lines.append("Self-check: %s" % json.dumps(payload["self_check"], sort_keys=True))
    lines.append("")
    return "\n".join(lines)


def _fmt(value):
    if value is None:
        return "-"
    if isinstance(value, float):
        return ("%.3f" % value).rstrip("0").rstrip(".")
    return str(value)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--make-fixture", action="store_true")
    parser.add_argument("--before-code-root", required=True)
    parser.add_argument("--after-code-root", default="")
    parser.add_argument("--fixture-dir", required=True)
    parser.add_argument("--scale", default="both", choices=("recorded", "current", "both"))
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--jobs-scratch", default="")
    parser.add_argument("--index-scratch", default="")
    parser.add_argument("--json", default="")
    parser.add_argument("--markdown", default="")
    parser.add_argument("--keep-scratch", action="store_true")
    args = parser.parse_args(argv)

    before = Path(args.before_code_root).resolve()
    fixture_dir = Path(args.fixture_dir).resolve()
    scales = DEFAULT_SCALES if args.scale == "both" else (args.scale,)

    if args.make_fixture:
        shutil.rmtree(fixture_dir, ignore_errors=True)
        fixture_dir.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [sys.executable, "-B", "-c", _BUILD, str(before), str(fixture_dir), ",".join(scales)],
            cwd=str(before), capture_output=True, text=True, timeout=1800,
            env={**os.environ, "PYTHONPATH": str(before / "utilities"),
                 "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": os.environ.get("TMPDIR", "/tmp")})
        if completed.returncode != 0:
            sys.stderr.write(completed.stdout[-4000:] + "\n" + completed.stderr[-8000:])
            return 1
        print(completed.stdout.strip().splitlines()[-1])

    fixture = Fixture(fixture_dir)
    scratch_root = Path(args.index_scratch or tempfile.mkdtemp(prefix="state-file-measure-"))
    scratch_root.mkdir(parents=True, exist_ok=True)
    try:
        before_result = measure(before, fixture, scratch_root / "before", scales, args.reps)
        after_result = (measure(Path(args.after_code_root).resolve(), fixture,
                                scratch_root / "after", scales, args.reps)
                        if args.after_code_root else None)
    finally:
        if not args.keep_scratch and not args.index_scratch:
            shutil.rmtree(scratch_root, ignore_errors=True)

    payload = {
        "schema": 1,
        "measured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "fixture": {"path": str(fixture_dir), **fixture.meta},
        "host": _host(),
        "page_cache": "warm",
        "reps": args.reps,
        "scales": list(scales),
        "before": before_result,
        "after": after_result or before_result,
        "self_check": {
            "before_equals_after_answers": (
                after_result is None
                or json.dumps(_answers(before_result), sort_keys=True)
                == json.dumps(_answers(after_result), sort_keys=True)),
            "all_correct": _all_correct(before_result) and (after_result is None or _all_correct(after_result)),
        },
    }
    text = json.dumps(payload, indent=2, sort_keys=True)
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(text + "\n", encoding="utf-8")
    if args.markdown:
        Path(args.markdown).parent.mkdir(parents=True, exist_ok=True)
        Path(args.markdown).write_text(_markdown(payload) + "\n", encoding="utf-8")
    print(json.dumps({"self_check": payload["self_check"],
                      "json": args.json, "markdown": args.markdown}, sort_keys=True))
    return 0 if payload["self_check"]["all_correct"] else 1


def _answers(result: dict) -> dict:
    answers = {}
    for name, scenario in result.get("capacity", {}).items():
        answers["capacity/" + name] = [run.get("answers") for run in scenario.get("raw", [])]
        answers["capacity-candidates/" + name] = [run.get("candidates") for run in scenario.get("raw", [])]
    for scale, data in result.get("scales", {}).items():
        for name, scenario in data.get("admission", {}).items():
            answers["admission/%s/%s" % (scale, name)] = scenario.get("answers")
    return answers


def _all_correct(result: dict) -> bool:
    for scenario in result.get("capacity", {}).values():
        if not scenario.get("ok", True):
            return False
    for data in result.get("scales", {}).values():
        for name, scenario in data.get("admission", {}).items():
            if not scenario.get("ok", True):
                return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
