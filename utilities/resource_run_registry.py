#!/usr/bin/env python3
"""Shared discovery and exact-identity liveness for detached lab resources."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dispatch_contract import resolve_agent_home as _resolve_agent_home  # noqa: E402

INDEX_SCHEMA = 1
REGISTRY_SCHEMA = 1
IDENTITY_KEYS = ("pid", "starttime", "command_hash")


def agent_home() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return _resolve_agent_home(runtime_pointer=codex_home / "hearting")


def default_index_path() -> Path:
    override = os.environ.get("AGENT_RESOURCE_RUN_INDEX")
    return Path(override).expanduser().resolve(strict=False) if override else (
        agent_home() / ".dispatch" / "resource-runs.index.json"
    )


def proc_identity(pid) -> dict | None:
    try:
        pid = int(pid)
        stat = Path(f"/proc/{pid}/stat")
        cmdline = Path(f"/proc/{pid}/cmdline")
        raw_stat = stat.read_text(encoding="utf-8")
        # comm is parenthesized and may itself contain spaces or ')'; parse
        # fields after the final ') ' so Linux field 22 stays rest[19].
        fields = raw_stat.rsplit(") ", 1)[1].split()
        command = cmdline.read_bytes()
        if len(fields) <= 19 or not command:
            return None
        return {
            "pid": pid,
            "starttime": fields[19],
            "command_hash": hashlib.sha256(command).hexdigest(),
        }
    except (OSError, TypeError, ValueError, IndexError):
        return None


def classify_identity(run: dict, identity_reader=proc_identity) -> tuple[str, dict | None, str]:
    """Return working/exited/stale without trusting the registry status word."""
    if not isinstance(run, dict) or any(run.get(key) in (None, "") for key in IDENTITY_KEYS):
        return "stale", None, "recorded-identity-incomplete"
    try:
        pid = int(run["pid"])
    except (TypeError, ValueError):
        return "stale", None, "recorded-pid-invalid"
    current = identity_reader(pid)
    if current is None:
        if Path(f"/proc/{pid}").exists():
            return "stale", None, "process-identity-unreadable"
        return "exited", None, "process-absent"
    if all(str(current[key]) == str(run[key]) for key in IDENTITY_KEYS):
        return "working", current, "exact-identity-match"
    return "stale", current, "process-identity-mismatch"


def is_alive(run: dict, identity_reader=proc_identity) -> bool:
    return classify_identity(run, identity_reader=identity_reader)[0] == "working"


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(payload, out, indent=2, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        try:
            dfd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def register_registry(registry, index_path=None, require_existing=True) -> dict:
    registry = Path(registry).expanduser().resolve(strict=False)
    if require_existing:
        data = json.loads(registry.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema_version") != REGISTRY_SCHEMA \
                or not isinstance(data.get("runs"), dict):
            raise ValueError("invalid resource-run registry")
    index = Path(index_path or default_index_path()).expanduser().resolve(strict=False)
    index.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(str(index) + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            payload = json.loads(index.read_text(encoding="utf-8")) if index.exists() else {
                "schema_version": INDEX_SCHEMA, "registries": {}
            }
        except (OSError, ValueError, TypeError):
            raise ValueError("malformed resource-run global index")
        if not isinstance(payload, dict) or payload.get("schema_version") != INDEX_SCHEMA \
                or not isinstance(payload.get("registries"), dict):
            raise ValueError("invalid resource-run global index")
        key = hashlib.sha256(str(registry).encode("utf-8")).hexdigest()
        now = time.time()
        old = payload["registries"].get(key)
        payload["registries"][key] = {
            "path": str(registry),
            "registered_at": old.get("registered_at", now) if isinstance(old, dict) else now,
            "updated_at": now,
        }
        _atomic_json(index, payload)
    return payload["registries"][key]


def indexed_paths(index_path=None) -> tuple[list[Path], list[dict]]:
    index = Path(index_path or default_index_path())
    diagnostics = []
    try:
        payload = json.loads(index.read_text(encoding="utf-8"))
        records = payload.get("registries") if isinstance(payload, dict) else None
        if payload.get("schema_version") != INDEX_SCHEMA or not isinstance(records, dict):
            raise ValueError("invalid-index-schema")
    except FileNotFoundError:
        return [], diagnostics
    except Exception as exc:
        return [], [{"kind": "malformed-index", "path": str(index), "error": str(exc)}]
    paths = []
    seen = set()
    for key, record in records.items():
        raw = record.get("path") if isinstance(record, dict) else None
        if not isinstance(raw, str) or not raw:
            diagnostics.append({"kind": "malformed-index-entry", "entry": str(key)})
            continue
        path = Path(raw).expanduser().resolve(strict=False)
        marker = str(path)
        if marker not in seen:
            seen.add(marker)
            paths.append(path)
    return paths, diagnostics


def _boot_epoch() -> float | None:
    try:
        for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _start_epoch(run: dict) -> float | None:
    value = run.get("started_at")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    try:
        boot = _boot_epoch()
        return boot + float(run["starttime"]) / float(os.sysconf("SC_CLK_TCK")) if boot else None
    except (KeyError, TypeError, ValueError, OSError):
        return None


def normalize_run(run_id: str, run: dict, registry: Path, identity_reader=proc_identity,
                  now=None) -> dict:
    now = time.time() if now is None else float(now)
    liveness, current, reason = classify_identity(run, identity_reader=identity_reader)
    cwd = run.get("cwd") if isinstance(run.get("cwd"), str) else ""
    log_path = run.get("log") or run.get("log_path")
    log_updated_at = None
    if isinstance(log_path, str) and log_path:
        try:
            log_updated_at = Path(log_path).stat().st_mtime
        except OSError:
            pass
    started_at = _start_epoch(run)
    end = now if liveness == "working" else (
        run.get("ended_at") if isinstance(run.get("ended_at"), (int, float)) else log_updated_at
    )
    elapsed_min = max(0, int((float(end) - started_at) / 60)) if started_at and end else None
    return {
        "job_type": "resource", "resource_class": "lab", "run_id": str(run_id),
        "cwd": cwd, "elapsed_min": elapsed_min, "liveness": liveness,
        "pid": run.get("pid"), "starttime": run.get("starttime"),
        "command_hash": run.get("command_hash"), "process_group": run.get("process_group"),
        "registry_status": run.get("status"), "registry_path": str(registry),
        "log_path": log_path, "log_updated_at": log_updated_at,
        "route": run.get("route"), "node": run.get("node"),
        "config_ref": run.get("config_ref"), "config_sha256": run.get("config_sha256"),
        "source_commit": run.get("source_commit"), "source_dirty": run.get("source_dirty"),
        "source_git_state": run.get("source_git_state"), "started_at": started_at,
        # Tracked-workflow projection (OPERATIONS §5.12): a resource row must expose why
        # it ended and who owns it, not just whether a PID is still there.
        "workflow_state": run.get("workflow_state"),
        "exit_code": run.get("exit_code"),
        "ended_at": run.get("ended_at") if isinstance(run.get("ended_at"), (int, float)) else None,
        "failure_class": run.get("failure_class"),
        "parent_attempt_id": run.get("parent_attempt_id"),
        "sentinel": run.get("sentinel"),
        "state_evidence": {"reason": reason, "current_identity": current},
    }


def scan(index_path=None, identity_reader=proc_identity, now=None) -> tuple[list[dict], list[dict]]:
    paths, diagnostics = indexed_paths(index_path)
    rows = []
    for registry in paths:
        try:
            payload = json.loads(registry.read_text(encoding="utf-8"))
            runs = payload.get("runs") if isinstance(payload, dict) else None
            if payload.get("schema_version") != REGISTRY_SCHEMA or not isinstance(runs, dict):
                raise ValueError("invalid-registry-schema")
        except Exception as exc:
            diagnostics.append({"kind": "malformed-registry", "path": str(registry),
                                "error": str(exc)})
            continue
        for run_id, run in runs.items():
            try:
                if not isinstance(run_id, str) or not isinstance(run, dict):
                    raise ValueError("invalid-run-row")
                rows.append(normalize_run(run_id, run, registry, identity_reader, now))
            except Exception as exc:
                diagnostics.append({"kind": "malformed-run", "path": str(registry),
                                    "run_id": str(run_id), "error": str(exc)})
    return rows, diagnostics


def counts(index_path=None) -> dict:
    rows, diagnostics = scan(index_path=index_path)
    result = {"working": 0, "stale": 0, "exited": 0, "malformed": len(diagnostics)}
    for row in rows:
        if row["liveness"] in result:
            result[row["liveness"]] += 1
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p_count = sub.add_parser("counts")
    p_count.add_argument("--index")
    p_count.add_argument("--format", choices=("json", "shell"), default="json")
    args = parser.parse_args(argv)
    if args.command == "counts":
        result = counts(args.index)
        if args.format == "shell":
            for key in ("working", "stale", "exited", "malformed"):
                print(f"{key}={result[key]}")
        else:
            print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
