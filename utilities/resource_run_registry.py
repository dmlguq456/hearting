#!/usr/bin/env python3
"""Shared discovery and exact-identity liveness for detached lab resources."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sys
import stat
import tempfile
import time
import uuid
import functools
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dispatch_contract import resolve_agent_home as _resolve_agent_home  # noqa: E402
from dispatch_contract import resolve_dispatch_state_root as _resolve_dispatch_state_root  # noqa: E402

INDEX_SCHEMA = 1
REGISTRY_SCHEMA = 1
IDENTITY_KEYS = ("pid", "starttime", "command_hash")


def agent_home() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return _resolve_agent_home(runtime_pointer=codex_home / "hearting")


def default_index_path() -> Path:
    override = os.environ.get("AGENT_RESOURCE_RUN_INDEX")
    return Path(override).expanduser().resolve(strict=False) if override else (
        _resolve_dispatch_state_root(agent_home()) / "resource-runs.index.json"
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
            **boot_identity(),
        }
    except (OSError, TypeError, ValueError, IndexError):
        return None


def boot_identity() -> dict:
    """Bind new local resource identities to their host and kernel lifetime."""
    try:
        boot = str(uuid.UUID(Path('/proc/sys/kernel/random/boot_id').read_text().strip()))
        machine = Path('/etc/machine-id').read_text().strip()
        host = Path('/proc/sys/kernel/hostname').read_text().strip()
        if len(machine) != 32 or not all(c in '0123456789abcdef' for c in machine) or not host:
            return {}
        return {'boot_id': boot, 'boot_host': machine + ':' + host}
    except (OSError, ValueError):
        return {}


@functools.lru_cache(maxsize=4)
def local_boot_history(current_boot: str) -> frozenset[str]:
    """Read this machine's existing boot journal, never another host's namespace."""
    try:
        result = subprocess.run(['journalctl', '--list-boots', '--no-pager', '--quiet'],
                                capture_output=True, text=True, timeout=2)
        if result.returncode != 0:
            return frozenset()
        boots = set()
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) > 1 and fields[0].lstrip('-').isdigit():
                boots.add(str(uuid.UUID(fields[1])))
        return frozenset(boots) if current_boot in boots else frozenset()
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return frozenset()


def legacy_resource_boot(run: dict) -> str | None:
    """Use the exact recorded owner's governor identity for pre-UUID resources."""
    try:
        from dispatch_contract import parse_registry_metadata
        wait = run['owner_wait']
        aid = run['parent_attempt_id']
        if (wait.get('parent_attempt_id') != aid or wait.get('jobs') != run['jobs']
                or not wait.get('owner_pid') or not wait.get('owner_start')):
            return None
        owners = []
        for line in Path(run['jobs']).read_text().splitlines():
            fields = line.split('\t')
            if len(fields) == 6:
                meta = parse_registry_metadata(fields[5])
                if meta.get('attempt_id') == aid:
                    owners.append(meta)
        if len(owners) != 1:
            return None
        owner = owners[0]
        if (owner.get('worker_type') != 'owner' or owner.get('owner_route_file') != run['route']
                or owner.get('owner_route_id') != wait.get('route_id')
                or owner.get('owner_route_hash') != wait.get('route_hash')
                or owner.get('pid') != str(wait['owner_pid'])
                or owner.get('pid_start') != str(wait['owner_start'])
                or owner.get('pid_ns') != run.get('pid_namespace')):
            return None
        path = Path(owner['artifact_root']) / '.runtime/model-worker-governor/state.json'
        state = json.loads(path.read_text())
        boots = set()
        for claim in state.get('claims', {}).values():
            identity = claim.get('claimant_identity') or {}
            if (str(identity.get('pid')) == owner['pid']
                    and str(identity.get('starttime')) == owner['pid_start']
                    and f"pid:[{identity.get('pid_namespace')}]" == owner['pid_ns']):
                boots.add(str(uuid.UUID(identity['boot_id'])))
        return boots.pop() if len(boots) == 1 else None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def reboot_evidence(run: dict) -> dict | None:
    """Positive local boot change; missing/unobservable identities are not death.

    Older local runner rows have no boot UUID. Their recorded start ticks beyond
    this kernel's uptime, together with a launch epoch before this boot and the
    exact owner boot in this host's journal, prove a local previous-boot run.
    """
    if run.get('boot_id') or run.get('boot_host'):
        current = boot_identity()
        try:
            old = str(uuid.UUID(run['boot_id']))
        except (KeyError, ValueError, TypeError):
            return None
        if current and run.get('boot_host') == current['boot_host'] and old != current['boot_id']:
            return {'previous_boot_id': old, **current}
        return None
    if (run.get('resource_policy') not in {'supervised-owner', 'verified-resume'}
            or not run.get('started_at') or not run.get('starttime') or not run.get('owner_wait')):
        return None
    try:
        boot = _boot_epoch()
        uptime = float(Path('/proc/uptime').read_text().split()[0])
        start, launched = int(run['starttime']), float(run['started_at'])
        if (boot and 0 < launched < boot and start > uptime * os.sysconf('SC_CLK_TCK')
                and run.get('pid_namespace') == os.readlink('/proc/self/ns/pid')):
            current = boot_identity()
            previous = legacy_resource_boot(run)
            if (current and previous and previous != current['boot_id']
                    and previous in local_boot_history(current['boot_id'])):
                return {'previous_boot_id': previous, **current, 'reason': 'boot-clock-reset'}
    except (OSError, KeyError, ValueError, TypeError, IndexError):
        pass
    return None


def _owned_wrapper_awaiting_reap(run, pid, identity_reader):
    """Recognize only this controller's exact unreaped child, never exit success.

    A zombie has no cmdline. Its stable kernel PID/start/parent/group tuple plus
    the still-exact controller distinguishes it from an unreadable live PID.
    The parent retains the actual Popen handle and owns the wait; observers do
    not reconstruct a handle or signal this PID.
    """
    owner = run.get("owner_wait")
    controller = run.get("launch_controller")
    if (run.get("resource_policy") != "supervised-owner"
            or not isinstance(owner, dict) or owner.get("launch_scope") != "codex-owner-controller"
            or run.get("launch_state") != "started" or not isinstance(controller, dict)
            or run.get("pid_namespace") != controller.get("pid_namespace")
            or not run.get("pid_namespace")):
        return False
    try:
        parent = int(controller["pid"])
        if (isinstance(controller["pid"], bool) or parent <= 0 or pid <= 0
                or int(run["process_group"]) != pid
                or os.readlink(f"/proc/{parent}/ns/pid") != run["pid_namespace"]):
            return False
        current_parent = identity_reader(parent)
        if not current_parent or any(str(current_parent[key]) != str(controller[key]) for key in IDENTITY_KEYS):
            return False
        path = Path(f"/proc/{pid}/stat")
        before = path.read_text(encoding="utf-8")
        fields = before.rsplit(") ", 1)[1].split()
        if (int(before.split(" ", 1)[0]) != pid or fields[0] != "Z"
                or int(fields[1]) != parent or int(fields[2]) != pid
                or fields[19] != str(run["starttime"])
                or Path(f"/proc/{pid}/cmdline").read_bytes() != b""):
            return False
        # A concurrent reap or PID reuse cannot turn this observation into a
        # terminal result. Recheck both kernel child and recorded parent.
        after = path.read_text(encoding="utf-8")
        return (after == before and identity_reader(parent) == current_parent)
    except (OSError, KeyError, TypeError, ValueError, IndexError):
        return False


def classify_identity(run: dict, identity_reader=proc_identity) -> tuple[str, dict | None, str]:
    """Return working/reaping/exited/stale without trusting registry status."""
    if not isinstance(run, dict) or any(run.get(key) in (None, "") for key in IDENTITY_KEYS):
        return "stale", None, "recorded-identity-incomplete"
    try:
        pid = int(run["pid"])
    except (TypeError, ValueError):
        return "stale", None, "recorded-pid-invalid"
    if reboot_evidence(run):
        return "exited", None, "host-reboot"
    if run.get("pid_namespace") is not None:
        try:
            if run["pid_namespace"] != os.readlink("/proc/self/ns/pid"):
                return "stale", None, "process-namespace-mismatch"
        except OSError:
            return "stale", None, "process-namespace-unreadable"
    current = identity_reader(pid)
    if current is None:
        if _owned_wrapper_awaiting_reap(run, pid, identity_reader):
            return "reaping", None, "owned-wrapper-awaiting-reap"
        if Path(f"/proc/{pid}").exists():
            return "stale", None, "process-identity-unreadable"
        return "exited", None, "process-absent"
    if all(str(current[key]) == str(run[key]) for key in IDENTITY_KEYS):
        return "working", current, "exact-identity-match"
    return "stale", current, "process-identity-mismatch"


def resource_never_started(run: dict) -> bool:
    """Read the runner's pre-release failure, never infer it from disappearance.

    Old direct launchers wrote this failure before publishing any identity.
    A controller's observed crash after claim uses the same failure class but
    retains its claim; only the launcher itself can record `not-started` there.
    """
    if (run.get("resource_policy") not in {"verified-resume", "supervised-owner"}
            or run.get("status") != "failed" or run.get("workflow_state") != "FAILED_RETRYABLE"
            or run.get("failure_class") != "resource-launch-incomplete"
            or run.get("exit_code") is not None or not run.get("sentinel")
            or run.get("cancel_requested") or run.get("parent_close_requested")):
        return False
    try:
        Path(run["sentinel"]).lstat()
    except FileNotFoundError:
        pass
    except (OSError, TypeError, ValueError):
        return False
    else:
        return False
    identity = any(run.get(key) is not None for key in IDENTITY_KEYS)
    if run.get("launch_state") == "not-started":
        return not identity or classify_identity(run)[0] == "exited"
    return (run.get("launch_state") is None and not identity
            and not run.get("launch_controller") and not run.get("launch_argv"))


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


def strict_json_loads(data):
    """Reject ambiguous object keys before any identity can be overwritten."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate-json-key:{key}")
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=unique)


def indexed_paths(index_path=None) -> tuple[list[Path], list[dict]]:
    index = Path(index_path or default_index_path())
    diagnostics = []
    try:
        payload = strict_json_loads(index.read_text(encoding="utf-8"))
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
        path = Path(raw).expanduser()
        if not path.is_absolute() or ".." in path.parts:
            diagnostics.append({"kind": "malformed-index-entry", "entry": str(key), "path": raw})
            continue
        marker = str(path)
        if marker not in seen:
            seen.add(marker)
            paths.append(path)
    return paths, diagnostics


def read_registry_source(path: Path) -> tuple[dict, bytes | None]:
    """Read once with path identity; only ordinary ENOENT is a negative fact.

    Inspect links before resolving so a dangling link cannot become a missing
    ordinary target. Directory identity (not mtime) also detects ancestor swaps.
    """
    path = path.expanduser().absolute()
    parents = []
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return {"kind": "missing", "path": str(path), "missing_at": str(current),
                    "reason": "missing-registry", "ancestors": parents}, None
        entry = {"path": str(current), "device": info.st_dev, "inode": info.st_ino}
        if stat.S_ISLNK(info.st_mode):
            entry["link"] = os.readlink(current)
            try:
                target = current.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise ValueError(f"registry-link-unverifiable:{current}") from exc
            entry["target"] = str(target)
        if current != path:
            if not current.is_dir():
                raise ValueError(f"registry-ancestor-not-directory:{current}")
            parents.append(entry)
    resolved = path.resolve(strict=True)
    # NONBLOCK also prevents a raced FIFO replacement from hanging the scan.
    fd = os.open(resolved, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        first = os.fstat(stream.fileno())
        if not stat.S_ISREG(first.st_mode):
            raise ValueError(f"registry-not-file:{path}")
        data = stream.read()
        last = os.fstat(stream.fileno())
    signature = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    if signature(first) != signature(last) or signature(last) != signature(resolved.stat()):
        raise ValueError(f"registry-changed-during-read:{path}")
    # The logical path and each ancestor must still name the object just read.
    for observed in [*parents, entry]:
        current_path = Path(observed["path"])
        current_info = current_path.lstat()
        if (current_info.st_dev, current_info.st_ino) != (observed["device"], observed["inode"]):
            raise ValueError(f"registry-path-changed-during-read:{path}")
        if "link" in observed and (os.readlink(current_path) != observed["link"]
                or str(current_path.resolve(strict=True)) != observed["target"]):
            raise ValueError(f"registry-link-changed-during-read:{path}")
    if path.resolve(strict=True) != resolved:
        raise ValueError(f"registry-path-changed-during-read:{path}")
    return {"kind": "file", "path": str(path), "resolved_path": str(resolved),
            "device": last.st_dev, "inode": last.st_ino, "ancestors": parents,
            "leaf": entry, "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
            "size": len(data)}, data


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
    log_mtime = None
    log_size = None
    if isinstance(log_path, str) and log_path:
        try:
            log_stat = Path(log_path).stat()
            log_mtime = log_stat.st_mtime
            log_size = log_stat.st_size
            if log_stat.st_size > 0:
                log_updated_at = log_mtime
        except OSError:
            pass
    started_at = _start_epoch(run)
    end = now if liveness == "working" else (
        run.get("ended_at") if isinstance(run.get("ended_at"), (int, float)) else log_mtime
    )
    elapsed_min = max(0, int((float(end) - started_at) / 60)) if started_at and end else None
    training_progress = None
    remote_training = []
    local_placement = None
    from resource_progress import read_progress
    progress = read_progress({**run, "run_id": str(run_id)}, now)
    if liveness == "working":
        from resource_progress import collect as collect_progress, remote_candidates
        from resource_placement import observe as observe_placement
        local_placement = observe_placement({**run, "run_id": str(run_id)})
        training_progress = collect_progress(run, registry, now)
        remote_training = remote_candidates(run, registry, now)
    return {
        "job_type": "resource", "resource_class": "lab", "run_id": str(run_id),
        "cwd": cwd, "elapsed_min": elapsed_min, "liveness": liveness,
        "pid": run.get("pid"), "starttime": run.get("starttime"),
        "command_hash": run.get("command_hash"), "process_group": run.get("process_group"),
        "command": (run["command"] if isinstance(run.get("command"), list)
                    and all(isinstance(arg, str) for arg in run["command"]) else None),
        "registry_status": run.get("status"), "registry_path": str(registry),
        "log_path": log_path, "log_updated_at": log_updated_at, "log_size": log_size,
        "route": run.get("route"), "node": run.get("node"),
        # Root-scoped quiescence consumes these as additive attribution inputs.
        # Existing resource-runner rows carry only route/node; newer ad-hoc
        # registrars may carry an explicit root or the route's expected identity.
        "artifact_root": run.get("artifact_root"),
        "route_file": run.get("route_file"),
        "route_id": run.get("route_id"), "route_hash": run.get("route_hash"),
        "route_node": run.get("route_node"),
        "config_ref": run.get("config_ref"), "config_sha256": run.get("config_sha256"),
        "source_commit": run.get("source_commit"), "source_dirty": run.get("source_dirty"),
        "source_git_state": run.get("source_git_state"), "started_at": started_at,
        "training_progress": training_progress,
        "progress_file": run.get("progress_file"), "progress": progress,
        "remote_training": remote_training,
        "local_placement": local_placement,
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


def scan(index_path=None, identity_reader=proc_identity, now=None,
         observed_sources: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    paths, diagnostics = indexed_paths(index_path)
    rows = []
    for registry in paths:
        try:
            source, data = read_registry_source(registry)
            if observed_sources is not None:
                observed_sources.append(source)
            if data is None:
                diagnostics.append({"kind": "missing-registry", "path": str(registry),
                                    "reason": "registered-path-absent", "blocking": False})
                continue
            payload = strict_json_loads(data)
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
    missing = sum(row.get("kind") == "missing-registry" for row in diagnostics)
    result = {"working": 0, "stale": 0, "exited": 0,
              "malformed": len(diagnostics) - missing, "missing": missing}
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
