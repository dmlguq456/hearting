#!/usr/bin/env python3
"""Publish and verify fail-closed Hearting quiescence evidence."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
SCHEMA_VERSION = 3
COUNT_KEYS = ("open_routes", "open_jobs", "open_dispatch_attempts")
RESOURCE_OPEN = {"open", "running", "pending", "working"}


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "utilities" / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"reader-unavailable:{filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ROUTES = _load("artifact_quiescence_routes", "capability-route.py")
RESOURCES = _load("artifact_quiescence_resources", "resource_run_registry.py")
DISPATCH = _load("artifact_quiescence_dispatch", "dispatch-registry.py")


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _file_row(path: Path) -> dict:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("source-not-file")
    data = resolved.read_bytes()
    return {"kind": "file", "path": str(resolved), "sha256": _digest_bytes(data), "size": len(data)}


def _directory_row(path: Path) -> dict:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("source-not-directory")
    names = sorted(item.name for item in resolved.iterdir())
    return {"kind": "directory", "path": str(resolved),
            "sha256": _digest_bytes("\n".join(names).encode())}


def _snapshot(rows: list[dict]) -> dict:
    ordered = sorted(rows, key=lambda row: (row["path"], row["kind"]))
    encoded = json.dumps(ordered, sort_keys=True, separators=(",", ":")).encode()
    return {"files": ordered, "snapshot_sha256": _digest_bytes(encoded)}


def _route_dirs(artifact_root: Path) -> list[Path]:
    return [artifact_root / ".runtime" / "routes", artifact_root,
            artifact_root / "routes", artifact_root / "_routes", artifact_root / ".routes"]


def _route_snapshot(artifact_root: Path) -> dict:
    rows = []
    for directory in _route_dirs(artifact_root):
        if directory.exists():
            rows.append(_directory_row(directory))
            rows.extend(_file_row(path) for path in sorted(directory.glob("*.json")))
    if not rows or not (artifact_root / ".runtime" / "routes").is_dir():
        raise ValueError("canonical-route-source-missing")
    return _snapshot(rows)


def _resource_snapshot(index_path: Path) -> dict:
    if not index_path.is_file():
        raise ValueError("resource-index-missing")
    paths, diagnostics = RESOURCES.indexed_paths(index_path)
    if diagnostics:
        raise ValueError("resource-index-unverifiable")
    return _snapshot([_file_row(index_path), *(_file_row(path) for path in paths)])


def _dispatch_snapshot(jobs: Path) -> dict:
    if not jobs.is_file():
        raise ValueError("dispatch-registry-missing")
    return _snapshot([_file_row(jobs)])


def _lock_probe(lock_path: Path) -> dict:
    """Observe ownership without treating the persistent lock file as state."""
    resolved = lock_path.expanduser().resolve(strict=False)
    if not resolved.exists():
        return {"kind": "lock", "path": str(resolved), "present": False,
                "held": False, "owner_state": "absent"}
    if not resolved.is_file():
        raise ValueError("lock-source-not-file")
    try:
        fd = os.open(resolved, os.O_RDWR | os.O_NOFOLLOW)
    except OSError as exc:
        raise ValueError("lock-source-unreadable") from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held = False
        except BlockingIOError:
            held = True
        content = os.pread(fd, 4096, 0).decode("utf-8", errors="strict")
        if "\x00" in content or "\n\n" in content:
            raise ValueError("lock-owner-malformed")
        return {"kind": "lock", "path": str(resolved), "present": held,
                "held": held, "owner_state": "held" if held else
                ("stale-owner" if content else "empty-unlocked")}
    except UnicodeError as exc:
        raise ValueError("lock-owner-malformed") from exc
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _source_snapshots(config: dict) -> dict:
    return {
        "routes": _route_snapshot(Path(config["artifact_root"])),
        "jobs": _resource_snapshot(Path(config["resource_index"])),
        "dispatch": _dispatch_snapshot(Path(config["dispatch_jobs"])),
        "lock": _lock_probe(Path(config["lock_path"])),
    }


def _dispatch_rows(jobs: Path) -> list[dict]:
    raw_lines = [line for line in jobs.read_text(encoding="utf-8", errors="strict").splitlines() if line]
    if any(len(line.split("\t")) != 6 for line in raw_lines):
        raise ValueError("dispatch-registry-malformed")
    rows = DISPATCH.read_rows(jobs)
    if len(rows) != len(raw_lines):
        raise ValueError("dispatch-registry-unverifiable")
    current = DISPATCH.current(rows)
    for row in current:
        if row["status"] in DISPATCH.OPEN and row.get("attempt_contract_status") != "current":
            raise ValueError("open-dispatch-contract-unverifiable")
    return current


def _observed_at(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("observation-time-offset-missing")
    return current.astimezone(timezone.utc).isoformat(timespec="microseconds")


def collect(config: dict, now: datetime | None = None) -> dict:
    before = _source_snapshots(config)
    route_diagnostics: list[dict] = []
    route_rows = ROUTES.route_status(config["artifact_root"], diagnostics=route_diagnostics)
    canonical_routes = Path(config["artifact_root"]) / ".runtime" / "routes"
    blocking_route_diagnostics = [
        row for row in route_diagnostics
        if Path(row.get("path", "")).parent == canonical_routes
        or "route" in Path(row.get("path", "")).name.lower()
    ]
    resource_rows, resource_diagnostics = RESOURCES.scan(index_path=config["resource_index"])
    if resource_diagnostics:
        raise ValueError("resource-source-unverifiable")
    dispatch_rows = _dispatch_rows(Path(config["dispatch_jobs"]))
    after = _source_snapshots(config)
    if before != after:
        raise ValueError("source-changed-during-observation")

    open_route_ids = {row.get("route_id") for row in route_rows if not row.get("closed")}
    open_route_ids.discard(None)
    open_jobs = sum(
        1 for row in resource_rows
        if row.get("liveness") == "working"
        or str(row.get("registry_status", "")).lower() in RESOURCE_OPEN
    )
    counts = {
        "open_routes": len(open_route_ids),
        "open_jobs": open_jobs,
        "open_dispatch_attempts": sum(1 for row in dispatch_rows if row["status"] in DISPATCH.OPEN),
    }
    lock_present = bool(after["lock"].get("held"))
    stamp = _observed_at(now)
    sources = {
        "routes": {"reader": "capability-route.py:route_status", "count": counts["open_routes"], **after["routes"]},
        "jobs": {"reader": "resource_run_registry.py:scan", "count": counts["open_jobs"], **after["jobs"]},
        "dispatch": {"reader": "dispatch-registry.py:current(read_rows)", "count": counts["open_dispatch_attempts"], **after["dispatch"]},
        "lock": {"reader": "flock(LOCK_EX|LOCK_NB)", "count": int(lock_present), **after["lock"]},
    }
    pending = sum(counts.values()) + int(lock_present)
    observation_valid = not blocking_route_diagnostics
    identity_seed = json.dumps({"observed_at": stamp, "sources": sources,
                                "nonce": uuid.uuid4().hex}, sort_keys=True).encode()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "observation_valid": observation_valid,
        "observation_id": _digest_bytes(identity_seed),
        "scope": config["scope"],
        "observed_at": stamp,
        **counts,
        "lock_present": lock_present,
        "pending": pending,
        "proven": observation_valid and pending == 0,
        "config": config,
        "sources": sources,
    }
    if blocking_route_diagnostics:
        payload["reason"] = "route-source-unverifiable"
        payload["source_diagnostics"] = [
            {"path": row.get("path"), "reason": row.get("reason")}
            for row in blocking_route_diagnostics
        ]
    return payload


def _atomic(path: Path, payload: dict) -> None:
    path = path.expanduser().resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def fixture_config(artifact_root: str, resource_index: str, dispatch_jobs: str) -> dict:
    return {
        "scope": "fixture",
        "artifact_root": str(Path(artifact_root).expanduser().resolve(strict=False)),
        "resource_index": str(Path(resource_index).expanduser().resolve(strict=False)),
        "dispatch_jobs": str(Path(dispatch_jobs).expanduser().resolve(strict=False)),
        "lock_path": str((Path(artifact_root) / ".pipeline-lock").expanduser().resolve(strict=False)),
    }


def live_config(cwd: str | None = None) -> dict:
    artifact_root = subprocess.check_output(
        [str(ROOT / "utilities" / "artifact-root.sh"), cwd or os.getcwd()], text=True,
        stderr=subprocess.DEVNULL,
    ).strip()
    jobs = os.environ.get("AGENT_DISPATCH_JOBS")
    if not jobs:
        raise ValueError("canonical-dispatch-registry-unavailable")
    return {
        "scope": "live",
        "artifact_root": str(Path(artifact_root).resolve(strict=False)),
        "resource_index": str(RESOURCES.default_index_path()),
        "dispatch_jobs": str(Path(jobs).expanduser().resolve(strict=False)),
        "lock_path": str((Path(artifact_root) / ".pipeline-lock").resolve(strict=False)),
    }


def publish(output: str, config: dict, now: datetime | None = None) -> dict:
    try:
        payload = collect(config, now)
    except Exception as exc:
        stamp = _observed_at(now)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "observation_valid": False,
            "observation_id": _digest_bytes(f"{stamp}:{uuid.uuid4().hex}".encode()),
            "scope": config.get("scope", "unknown"),
            "observed_at": stamp,
            "open_routes": 0,
            "open_jobs": 0,
            "open_dispatch_attempts": 0,
            "lock_present": False,
            "pending": 0,
            "proven": False,
            "config": config,
            "sources": {},
            "reason": str(exc).replace("\n", " ")[:160],
        }
    _atomic(Path(output), payload)
    return payload


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp-missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp-offset-missing")
    return parsed.astimezone(timezone.utc)


def validate(path: str, max_age: int = 300, now: datetime | None = None,
             allow_fixture: bool = False) -> dict:
    result = {"proven": False, "evidence": str(Path(path).expanduser().resolve(strict=False))}
    reasons: list[str] = []
    try:
        if not isinstance(max_age, int) or isinstance(max_age, bool) or max_age < 0:
            raise ValueError("max-age-invalid")
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("evidence-schema-invalid")
        if payload.get("observation_valid") is not True:
            raise ValueError("observation-invalid")
        scope = payload.get("scope")
        if scope not in {"live", "fixture"} or (scope == "fixture" and not allow_fixture):
            raise ValueError("evidence-scope-not-authorized")
        observed = _time(payload.get("observed_at"))
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if observed > current + timedelta(seconds=30):
            raise ValueError("evidence-from-future")
        if current - observed > timedelta(seconds=max_age):
            raise ValueError("evidence-stale")
        for key in COUNT_KEYS:
            value = payload.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("count-invalid")
        if payload.get("pending") != sum(payload[key] for key in COUNT_KEYS):
            if payload.get("pending") != sum(payload[key] for key in COUNT_KEYS) + int(payload.get("lock_present") is True):
                raise ValueError("pending-sum-invalid")
        if not isinstance(payload.get("lock_present"), bool):
            raise ValueError("lock-state-invalid")
        if payload.get("proven") is not (payload["pending"] == 0):
            raise ValueError("published-proof-invalid")
        config = payload.get("config")
        if not isinstance(config, dict):
            raise ValueError("source-config-missing")
        if scope == "live" and config != live_config():
            raise ValueError("live-source-config-changed")
        current_payload = collect(config, current)
        if any(current_payload[key] != payload[key] for key in (*COUNT_KEYS, "lock_present", "pending")):
            raise ValueError("source-count-changed")
        if current_payload["sources"] != payload.get("sources"):
            raise ValueError("source-evidence-changed")
        if payload["pending"] != 0:
            raise ValueError("pending-work-nonzero")
        result.update({"proven": True, "pending": 0, "scope": scope,
                       "observation_id": payload.get("observation_id")})
    except Exception as exc:
        reasons.append(str(exc).replace("\n", " ")[:160])
    if reasons:
        result["reasons"] = reasons
    return result


def pair(first: str, second: str, fold_start: str, fold_end: str, max_age: int = 300,
         now: datetime | None = None, allow_fixture: bool = False) -> dict:
    current = now or datetime.now(timezone.utc)
    first_result = validate(first, max_age, current, allow_fixture)
    second_result = validate(second, max_age, current, allow_fixture)
    result = {"proven": False, "first": first_result, "second": second_result,
              "fold_start": fold_start, "fold_end": fold_end}
    reasons = []
    try:
        first_path = Path(first).expanduser().resolve(strict=True)
        second_path = Path(second).expanduser().resolve(strict=True)
        if first_path == second_path:
            raise ValueError("samples-not-distinct")
        first_payload = json.loads(first_path.read_text(encoding="utf-8"))
        second_payload = json.loads(second_path.read_text(encoding="utf-8"))
        before = _time(first_payload.get("observed_at"))
        after = _time(second_payload.get("observed_at"))
        start = _time(fold_start)
        end = _time(fold_end)
        if first_payload.get("observation_id") == second_payload.get("observation_id") or not before < after:
            raise ValueError("samples-not-independent")
        if first_payload.get("config") != second_payload.get("config"):
            raise ValueError("sample-source-config-mismatch")
        if not before <= start <= end <= after:
            raise ValueError("fold-not-bracketed")
        if not first_result.get("proven") or not second_result.get("proven"):
            raise ValueError("sample-not-proven")
        result["proven"] = True
    except Exception as exc:
        reasons.append(str(exc).replace("\n", " ")[:160])
    if reasons:
        result["reasons"] = reasons
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="operation", required=True)
    observe = sub.add_parser("observe")
    observe.add_argument("--output", required=True)
    observe.add_argument("--cwd")
    observe.add_argument("--fixture", action="store_true")
    observe.add_argument("--artifact-root")
    observe.add_argument("--resource-index")
    observe.add_argument("--dispatch-jobs")
    check = sub.add_parser("validate")
    check.add_argument("evidence")
    check.add_argument("--max-age", type=int, default=300)
    check.add_argument("--allow-fixture", action="store_true")
    fold = sub.add_parser("pair")
    fold.add_argument("first")
    fold.add_argument("second")
    fold.add_argument("--fold-start", required=True)
    fold.add_argument("--fold-end", required=True)
    fold.add_argument("--max-age", type=int, default=300)
    fold.add_argument("--allow-fixture", action="store_true")
    args = parser.parse_args()

    if args.operation == "observe":
        fixture_values = (args.artifact_root, args.resource_index, args.dispatch_jobs)
        if args.fixture:
            if not all(fixture_values):
                parser.error("fixture observation requires all three source paths")
            config = fixture_config(*fixture_values)
        else:
            if any(fixture_values):
                parser.error("live observation does not accept source overrides")
            config = live_config(args.cwd)
        result = publish(args.output, config)
    elif args.operation == "validate":
        result = validate(args.evidence, args.max_age, allow_fixture=args.allow_fixture)
    else:
        result = pair(args.first, args.second, args.fold_start, args.fold_end,
                      args.max_age, allow_fixture=args.allow_fixture)
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("proven") else 1


if __name__ == "__main__":
    raise SystemExit(main())
