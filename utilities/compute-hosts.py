#!/usr/bin/env python3
"""Inventory, probe, and run work on the operator's own compute hosts.

Sessions run on one machine while training and evaluation belong on whichever
host has the right GPUs. Without a recorded inventory every session rediscovers
addresses, ports, and environments by scanning, and every long run invents its
own answer to "where do the logs go" and "how do I get back to it".

This holds the static half in one user-owned file and measures the rest:

    list    inventory plus reachability, GPU load/VRAM, and exact process owners
    probe   the live GPU/process measurement alone, for one host or all of them
    claim   bind a detached root PID to its exact launcher session identity
    run     start a detached command on a host under a stable run id
    runs    what has been started, and which of those are still alive
    tail    read a run's log from any host that shares the run root
    stop    end a running detached command

A run is detached on purpose: the session that starts it may end long before
the work does, and any later session -- on any host sharing the run root -- can
follow it by id. Only the standard library is used, matching the rest of the
harness utilities.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import fcntl
import importlib.util
import json
import os
import shlex
import socket
import subprocess
import sys
from pathlib import Path

SCHEMA_VERSION = 1
CONNECT_TIMEOUT = 8
PROBE_TIMEOUT = 40
GPU_PROBE_TIMEOUT = 3
LOCAL = "local"
OWNER_CLAIMS = ".process-owners.json"
OWNER_CLAIMS_LOCK = ".process-owners.lock"
OWNER_CLAIMS_SCHEMA = 1


def _parser_module():
    """Reuse the harness YAML subset parser rather than adding a dependency."""
    path = Path(__file__).resolve().parent / "dispatch-defaults.py"
    spec = importlib.util.spec_from_file_location("_hearting_yaml", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def config_path() -> Path:
    override = os.environ.get("COMPUTE_HOSTS_CONFIG")
    if override:
        return Path(override).expanduser()
    root = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(root).expanduser() / "hearting" / "compute-hosts.yaml"


class ConfigError(RuntimeError):
    pass


def load_config():
    path = config_path()
    if path.is_symlink() or not path.is_file():
        raise ConfigError(f"compute host inventory is not initialized: {path}")
    try:
        data = _parser_module().parse_yaml_subset(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ConfigError(f"invalid compute host inventory: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise ConfigError(f"unsupported inventory schema: {path}")
    hosts = data.get("hosts")
    if not isinstance(hosts, dict) or not hosts:
        raise ConfigError("inventory declares no hosts")
    run_root = data.get("run_root")
    if not isinstance(run_root, str) or not run_root.startswith("/"):
        raise ConfigError("run_root must be an absolute path")
    for name, host in hosts.items():
        if not isinstance(host, dict) or not host.get("ssh_host"):
            raise ConfigError(f"host {name} declares no ssh_host")
    return {"run_root": Path(run_root), "hosts": hosts}


def _claim_path(run_root):
    return Path(run_root) / OWNER_CLAIMS


def _load_claims(run_root):
    path = _claim_path(run_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    if not isinstance(payload, dict) or payload.get("schema_version") != OWNER_CLAIMS_SCHEMA:
        return []
    rows = payload.get("claims")
    return [dict(row) for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _record_claim(run_root, claim):
    """Atomically replace one exact host/root identity claim under the shared run root."""
    root = Path(run_root)
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / OWNER_CLAIMS_LOCK
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        claims = _load_claims(root)
        key = (claim["host"], claim["root_pid"], claim["root_start"])
        claims = [row for row in claims
                  if (row.get("host"), row.get("root_pid"), row.get("root_start")) != key]
        claims.append(dict(claim))
        payload = {"schema_version": OWNER_CLAIMS_SCHEMA, "claims": claims[-1024:]}
        temp = root / (OWNER_CLAIMS + ".tmp.%d" % os.getpid())
        try:
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1,
                                       sort_keys=True) + "\n", encoding="utf-8")
            os.replace(temp, _claim_path(root))
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def is_self(host):
    """True when this entry describes the machine we are already running on.

    The inventory is meant to be byte-identical on every machine, so which
    entry is local has to be discovered rather than written down: marking one
    host `local` would make the file machine-specific and turn moving the
    session host into an edit on every server. A declared `hostname` is matched
    against this machine's own, since an inventory label (`moving4`) and the
    system hostname (`workstation`) need not agree.
    """
    if host.get("ssh_host") == LOCAL:
        return True
    declared = host.get("hostname")
    return bool(declared) and declared == socket.gethostname()


def ssh_prefix(host):
    """Argv prefix that runs a command on this host, locally or over SSH."""
    if is_self(host):
        return []
    target = host["ssh_host"]
    user = host.get("ssh_user")
    argv = ["ssh", "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={CONNECT_TIMEOUT}"]
    if host.get("ssh_port"):
        argv += ["-p", str(host["ssh_port"])]
    argv.append(f"{user}@{target}" if user else target)
    return argv


def remote(host, script, *, timeout=PROBE_TIMEOUT):
    """Run one shell script on a host and return the completed process.

    ssh joins its command arguments and hands the result to a remote shell,
    which parses them a second time. The script therefore has to arrive as one
    already-quoted word, or any quoting inside it is re-interpreted and a
    command containing its own quotes silently falls apart.
    """
    prefix = ssh_prefix(host)
    argv = (prefix + [f"bash -lc {shlex.quote(script)}"] if prefix
            else ["bash", "-lc", script])
    try:
        return subprocess.run(argv, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 255, "", "timed out")


PROBE_SCRIPT = r"""
python3 - <<'PY'
import csv
import hashlib
import io
import json
import os
import socket
import subprocess
import time
from pathlib import Path

try:
    OWNER_CLAIMS = json.loads(os.environ.get("HEARTING_OWNER_CLAIMS_JSON", "[]"))
except (TypeError, ValueError):
    OWNER_CLAIMS = []
if not isinstance(OWNER_CLAIMS, list):
    OWNER_CLAIMS = []


def smi(query):
    try:
        result = subprocess.run(
            ["nvidia-smi", query, "--format=csv,noheader,nounits"],
            text=True, capture_output=True, timeout=1.3,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if result.returncode:
        return None, (result.stderr or result.stdout or "nvidia-smi failed").strip()[:160]
    return list(csv.reader(io.StringIO(result.stdout))), None


def integer(value):
    value = str(value or "").strip()
    if not value or value.lower() in {"n/a", "[n/a]", "not supported"}:
        return None
    try:
        return int(float(value.split()[0]))
    except (ValueError, IndexError):
        return None


def proc_stat(pid):
    try:
        raw = Path("/proc") / str(pid) / "stat"
        text = raw.read_text(encoding="utf-8", errors="replace")
        rest = text[text.rfind(")") + 2:].split()
        return {"ppid": int(rest[1]), "start": int(rest[19])}
    except (OSError, ValueError, IndexError):
        return None


def proc_cmdline_sha256(pid):
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return None
    return hashlib.sha256(raw).hexdigest()


def cpu_sample():
    def read():
        try:
            fields = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
            values = [int(value) for value in fields]
        except (OSError, ValueError, IndexError):
            return None
        if len(values) < 4:
            return None
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return sum(values), idle

    before = read()
    if before is None:
        return None
    time.sleep(0.10)
    after = read()
    if after is None:
        return None
    total = after[0] - before[0]
    idle = after[1] - before[1]
    if total <= 0:
        return None
    return max(0, min(100, int(round(100.0 * (total - idle) / total))))


def same_euid(pid):
    try:
        for line in (Path("/proc") / str(pid) / "status").read_text().splitlines():
            if line.startswith("Uid:"):
                fields = line.split()
                return len(fields) >= 3 and int(fields[2]) == os.geteuid()
    except (OSError, ValueError):
        pass
    return False


ENV_KEYS = {
    "AGENT_DISPATCH_ATTEMPT_ID", "AGENT_DISPATCH_SELF_SLUG",
    "HEARTING_COMPUTE_RUN_ID", "HEARTING_COMPUTE_HOST",
    "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID",
    "OPENCODE_SESSION_ID",
}


def identity_env(pid):
    values = {}
    try:
        with (Path("/proc") / str(pid) / "environ").open("rb") as handle:
            raw = handle.read(1024 * 1024)
    except OSError:
        return values
    for item in raw.split(b"\0"):
        key, sep, value = item.partition(b"=")
        if not sep:
            continue
        decoded_key = key.decode("utf-8", errors="ignore")
        if decoded_key in ENV_KEYS:
            values[decoded_key] = value.decode("utf-8", errors="replace")[:256]
    return values


def harness_process(pid):
    names = {"claude": "claude", "codex": "codex", "opencode": "opencode"}
    try:
        comm = (Path("/proc") / str(pid) / "comm").read_text().strip().lower()
    except OSError:
        comm = ""
    if comm in names:
        return names[comm]
    try:
        with (Path("/proc") / str(pid) / "cmdline").open("rb") as handle:
            argv0 = handle.read(4096).split(b"\0", 1)[0]
        base = os.path.basename(argv0.decode("utf-8", errors="ignore")).lower()
    except OSError:
        base = ""
    return names.get(base)


def safe_label(value, fallback):
    text = str(value or fallback)
    return "".join(c if c.isalnum() or c in "._-" else "-" for c in text)[:64]


def process_owner(pid, expected_start):
    candidates = {"job": [], "run": [], "session": [], "harness": []}
    seen = set()
    current = pid
    depth = 0
    while current > 0 and current not in seen and depth < 128:
        seen.add(current)
        stat = proc_stat(current)
        if stat is None or not same_euid(current):
            break
        env = identity_env(current)
        attempt = env.get("AGENT_DISPATCH_ATTEMPT_ID")
        if attempt:
            slug = safe_label(env.get("AGENT_DISPATCH_SELF_SLUG"), attempt[:12])
            candidates["job"].append({
                "kind": "job", "id": attempt, "label": "job:" + slug,
                "harness": None, "evidence_pid": current,
                "evidence_start": stat["start"], "ancestry_depth": depth,
                "source": "environment+ancestry",
            })
        run_id = env.get("HEARTING_COMPUTE_RUN_ID")
        if run_id:
            candidates["run"].append({
                "kind": "run", "id": run_id,
                "label": "run:" + safe_label(run_id, "run"), "harness": None,
                "evidence_pid": current, "evidence_start": stat["start"],
                "ancestry_depth": depth, "source": "environment+ancestry",
            })
        sessions = []
        for key, harness in (("CLAUDE_CODE_SESSION_ID", "claude"),
                             ("CODEX_THREAD_ID", "codex"),
                             ("CODEX_SESSION_ID", "codex"),
                             ("OPENCODE_SESSION_ID", "opencode")):
            sid = env.get(key)
            if sid:
                sessions.append((harness, sid))
        for harness, sid in set(sessions):
            candidates["session"].append({
                "kind": "session", "id": sid,
                "label": "%s:%s" % (harness, safe_label(sid[:8], "session")),
                "harness": harness, "evidence_pid": current,
                "evidence_start": stat["start"], "ancestry_depth": depth,
                "source": "environment+ancestry",
            })
        for claim in OWNER_CLAIMS:
            if not isinstance(claim, dict):
                continue
            owner = claim.get("owner")
            if (claim.get("root_pid") != current or claim.get("root_start") != stat["start"]
                    or not isinstance(owner, dict) or owner.get("kind") != "session"):
                continue
            expected_hash = claim.get("root_cmdline_sha256")
            if (not isinstance(expected_hash, str)
                    or proc_cmdline_sha256(current) != expected_hash):
                continue
            harness = owner.get("harness")
            sid = owner.get("id")
            if harness not in {"claude", "codex", "opencode"} or not isinstance(sid, str):
                continue
            candidates["session"].append({
                "kind": "session", "id": sid,
                "label": "%s:%s" % (harness, safe_label(sid[:8], "session")),
                "harness": harness, "evidence_pid": current,
                "evidence_start": stat["start"], "ancestry_depth": depth,
                "source": "persistent-claim+ancestry",
            })
        harness = harness_process(current)
        if harness:
            candidates["harness"].append({
                "kind": "harness", "id": "%s:%s" % (current, stat["start"]),
                "label": "%s:pid%s" % (harness, current), "harness": harness,
                "evidence_pid": current, "evidence_start": stat["start"],
                "ancestry_depth": depth, "source": "process-ancestry",
            })
        verified = proc_stat(current)
        if verified is None or verified["start"] != stat["start"]:
            return None, "ancestor-reused-or-gone"
        current = stat["ppid"]
        depth += 1

    final_stat = proc_stat(pid)
    if final_stat is None or final_stat["start"] != expected_start:
        return None, "pid-reused-or-gone"
    for kind in ("job", "run", "session", "harness"):
        if not candidates[kind]:
            continue
        nearest = min(candidate["ancestry_depth"] for candidate in candidates[kind])
        unique = {}
        for candidate in candidates[kind]:
            if candidate["ancestry_depth"] != nearest:
                continue
            unique.setdefault((candidate["kind"], candidate.get("harness"),
                               candidate["id"]), candidate)
        if len(unique) == 1:
            return next(iter(unique.values())), None
        if len(unique) > 1:
            return None, "ambiguous-" + kind
    return None, "no-exact-owner"


payload = {
    "hostname": socket.gethostname(), "load": None,
    "cpu_count": os.cpu_count(), "cpu_utilization_pct": cpu_sample(), "gpus": [],
    "unmatched_processes": [], "observed_at": time.time(),
}
try:
    payload["load"] = " ".join(Path("/proc/loadavg").read_text().split()[:3])
except OSError:
    pass

if not any(os.access(os.path.join(path, "nvidia-smi"), os.X_OK)
           for path in os.environ.get("PATH", "").split(os.pathsep)):
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    raise SystemExit(0)

gpu_rows, gpu_error = smi("--query-gpu=index,uuid,name,utilization.gpu,memory.total,memory.used")
if gpu_rows is None:
    payload["gpu_error"] = gpu_error
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    raise SystemExit(0)

by_uuid = {}
for row in gpu_rows:
    if len(row) != 6:
        continue
    index, uuid, name, util, total, used = (part.strip() for part in row)
    try:
        index = int(index)
    except ValueError:
        continue
    gpu = {
        "index": index, "uuid": uuid or None, "name": name or None,
        "utilization_gpu_pct": integer(util), "memory_total_mib": integer(total),
        "memory_used_mib": integer(used), "processes": [],
    }
    payload["gpus"].append(gpu)
    if uuid:
        by_uuid[uuid] = gpu

process_rows, process_error = smi("--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory")
if process_rows is None:
    payload["process_error"] = process_error
else:
    for row in process_rows:
        if len(row) != 4:
            continue
        uuid, raw_pid, process_name, used = (part.strip() for part in row)
        try:
            pid = int(raw_pid)
        except ValueError:
            continue
        stat = proc_stat(pid)
        owner, reason = (process_owner(pid, stat["start"]) if stat is not None
                         else (None, "process-unavailable"))
        process = {
            "gpu_uuid": uuid or None, "pid": pid,
            "proc_start": stat["start"] if stat is not None else None,
            "process_name": process_name or None, "used_memory_mib": integer(used),
            "owner": owner, "attribution_reason": reason,
        }
        gpu = by_uuid.get(uuid)
        if gpu is None:
            payload["unmatched_processes"].append(process)
        else:
            gpu["processes"].append(process)

payload["gpus"].sort(key=lambda gpu: gpu["index"])
for gpu in payload["gpus"]:
    gpu["processes"].sort(
        key=lambda proc: (proc["used_memory_mib"] is None,
                          -(proc["used_memory_mib"] or 0), proc["pid"]))
print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
PY
"""


def probe_host(name, host, owner_claims=None):
    observed_at = datetime.datetime.now().timestamp()
    claims_json = json.dumps(list(owner_claims or ()), ensure_ascii=False,
                             separators=(",", ":"))
    script = "export HEARTING_OWNER_CLAIMS_JSON=%s\n%s" % (
        shlex.quote(claims_json), PROBE_SCRIPT)
    result = remote(host, script, timeout=GPU_PROBE_TIMEOUT)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        return {"host": name, "reachable": False,
                "detail": (detail[-1][:120] if detail else "unreachable"),
                "gpus": [], "observed_at": observed_at}
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, TypeError, ValueError):
        return {"host": name, "reachable": False, "detail": "invalid probe output",
                "gpus": [], "observed_at": observed_at}
    if not isinstance(payload, dict) or not isinstance(payload.get("gpus"), list):
        return {"host": name, "reachable": False, "detail": "invalid probe payload",
                "gpus": [], "observed_at": observed_at}
    gpus = []
    for gpu in payload.get("gpus", []):
        if not isinstance(gpu, dict) or not isinstance(gpu.get("index"), int):
            continue
        gpu = dict(gpu)
        total, used = gpu.get("memory_total_mib"), gpu.get("memory_used_mib")
        gpu["total_mib"] = total
        gpu["used_mib"] = used
        gpu["free_mib"] = total - used if isinstance(total, int) and isinstance(used, int) else None
        if not isinstance(gpu.get("processes"), list):
            gpu["processes"] = []
        gpus.append(gpu)
    row = {"host": name, "reachable": True,
           "hostname": payload.get("hostname"), "load": payload.get("load"),
           "cpu_count": payload.get("cpu_count"),
           "cpu_utilization_pct": payload.get("cpu_utilization_pct"),
           "gpus": gpus, "observed_at": payload.get("observed_at") or observed_at,
           "unmatched_processes": payload.get("unmatched_processes") or []}
    if payload.get("gpu_error"):
        row["detail"] = str(payload["gpu_error"])[:120]
    if payload.get("process_error"):
        row["process_detail"] = str(payload["process_error"])[:120]
    return row


def _probe_selected(selected, run_root=None):
    """Probe hosts concurrently while preserving the inventory's declared order."""
    if not selected:
        return []
    claims = _load_claims(run_root) if run_root is not None else []
    claims_by_host = {}
    for claim in claims:
        if isinstance(claim.get("host"), str):
            claims_by_host.setdefault(claim["host"], []).append(claim)
    workers = min(8, len(selected))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(
            lambda item: probe_host(item[0], item[1], claims_by_host.get(item[0], ())),
            selected))


def _select(config, names):
    hosts = config["hosts"]
    if not names:
        return list(hosts.items())
    missing = [n for n in names if n not in hosts]
    if missing:
        raise ConfigError(f"unknown host(s): {', '.join(missing)}")
    return [(n, hosts[n]) for n in names]


def cmd_list(args):
    config = load_config()
    selected = _select(config, args.hosts)
    live_rows = ({row["host"]: row
                  for row in _probe_selected(selected, config["run_root"])}
                 if not args.static else {})
    rows = []
    for name, host in selected:
        row = {"host": name, "ssh": host.get("ssh_host"),
               "port": host.get("ssh_port"), "conda": host.get("conda"),
               "note": host.get("note"), "self": is_self(host)}
        row.update(live_rows[name] if not args.static
                   else {"reachable": None, "gpus": []})
        rows.append(row)
    if args.json:
        print(json.dumps({"run_root": str(config["run_root"]), "hosts": rows},
                         ensure_ascii=False, sort_keys=True))
        return 0
    print(f"run root: {config['run_root']}")
    for row in rows:
        if row.get("reachable") is False:
            print(f"  {row['host']:<10} unreachable  ({row.get('detail', '')})")
            continue
        if row.get("reachable") is None:
            here = "*" if row.get("self") else " "
            print(f" {here}{row['host']:<10} {row['ssh']}  {row.get('note') or ''}")
            continue
        summary = ", ".join(
            f"{g['index']}:{g['name'].replace('NVIDIA ', '')} "
            f"{g['used_mib'] / 1024:.1f}/{g['total_mib'] / 1024:.0f}G "
            f"{g.get('utilization_gpu_pct') if g.get('utilization_gpu_pct') is not None else '—'}%"
            for g in row["gpus"] if g.get("used_mib") is not None
            and g.get("total_mib") is not None) or "no gpu"
        here = "*" if row.get("self") else " "
        cpu = row.get("cpu_utilization_pct")
        cpu_text = "%s%%" % cpu if isinstance(cpu, int) else "—"
        print(f" {here}{row['host']:<10} up   cpu {cpu_text:<4} "
              f"load {row.get('load', '?')}  | {summary}")
    return 0


def cmd_probe(args):
    config = load_config()
    results = _probe_selected(_select(config, args.hosts), config["run_root"])
    if args.json:
        print(json.dumps(results, ensure_ascii=False, sort_keys=True))
    else:
        for row in results:
            state = "up" if row["reachable"] else f"down ({row.get('detail', '')})"
            print(f"{row['host']:<10} {state}")
            for gpu in row.get("gpus", []):
                util = gpu.get("utilization_gpu_pct")
                print(f"    gpu{gpu['index']} {gpu['name']}: "
                      f"{gpu.get('used_mib')}/{gpu.get('total_mib')} MiB used, "
                      f"{util if util is not None else '—'}% util")
    return 0 if all(r["reachable"] for r in results) else 1


CLAIM_IDENTITY_SCRIPT = r"""
python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path

pid = int(os.environ["HEARTING_CLAIM_PID"])
root = Path("/proc") / str(pid)
try:
    text = (root / "stat").read_text(encoding="utf-8", errors="replace")
    rest = text[text.rfind(")") + 2:].split()
    start = int(rest[19])
    cmdline = (root / "cmdline").read_bytes()
    status = (root / "status").read_text(encoding="utf-8", errors="replace")
except (OSError, ValueError, IndexError) as exc:
    raise SystemExit("process unavailable: %s" % exc)
effective = None
for line in status.splitlines():
    if line.startswith("Uid:"):
        fields = line.split()
        effective = int(fields[2]) if len(fields) >= 3 else None
        break
if effective != os.geteuid():
    raise SystemExit("process belongs to another effective uid")
print(json.dumps({
    "pid": pid, "start": start,
    "cmdline_sha256": hashlib.sha256(cmdline).hexdigest(),
}, sort_keys=True))
PY
"""


def cmd_claim(args):
    config = load_config()
    (name, host), = _select(config, [args.host])
    if args.harness not in {"claude", "codex", "opencode"}:
        raise ConfigError("claim harness must be claude, codex, or opencode")
    session_id = str(args.session or "").strip()
    if not session_id or len(session_id) > 256:
        raise ConfigError("claim session id must contain 1..256 characters")
    script = "export HEARTING_CLAIM_PID=%d\n%s" % (args.pid, CLAIM_IDENTITY_SCRIPT)
    result = remote(host, script, timeout=CONNECT_TIMEOUT)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "identity probe failed").strip()
        raise ConfigError("cannot claim %s pid %d: %s" % (name, args.pid, detail[:200]))
    try:
        identity = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, TypeError, ValueError) as exc:
        raise ConfigError("invalid claim identity response") from exc
    if (identity.get("pid") != args.pid or not isinstance(identity.get("start"), int)
            or not isinstance(identity.get("cmdline_sha256"), str)):
        raise ConfigError("incomplete claim identity response")
    claim = {
        "host": name,
        "root_pid": args.pid,
        "root_start": identity["start"],
        "root_cmdline_sha256": identity["cmdline_sha256"],
        "owner": {
            "kind": "session", "id": session_id, "harness": args.harness,
            "label": "%s:%s" % (args.harness, session_id[:8]),
        },
        "recorded_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    _record_claim(config["run_root"], claim)
    if args.json:
        print(json.dumps(claim, ensure_ascii=False, sort_keys=True))
    else:
        print("claimed %s pid %d -> %s" % (name, args.pid, claim["owner"]["label"]))
    return 0


def _run_id(host_name, label, now, run_root=None):
    stamp = now.strftime("%Y%m%d-%H%M%S")
    # A run id doubles as a tmux session name, which cannot contain '.' or ':'.
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in (label or ""))
    base = f"{host_name}-{stamp}" + (f"-{safe}" if safe else "")
    if run_root is None:
        return base
    # Two launches within the same second would otherwise share a directory and
    # overwrite each other's log.
    candidate, suffix = base, 2
    while (Path(run_root) / candidate).exists():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def cmd_run(args):
    config = load_config()
    (name, host), = _select(config, [args.host])
    command = list(args.command or [])
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise ConfigError("a command is required after --")
    run_id = _run_id(name, args.name, datetime.datetime.now(),
                     run_root=config["run_root"])
    run_dir = config["run_root"] / run_id
    rendered = " ".join(shlex.quote(part) for part in command)

    setup = [
        f"export HEARTING_COMPUTE_RUN_ID={shlex.quote(run_id)}",
        f"export HEARTING_COMPUTE_HOST={shlex.quote(name)}",
    ]
    workdir = args.cwd or host.get("workdir")
    if workdir:
        setup.append(f"cd {shlex.quote(workdir)}")
    env = args.env or host.get("conda_env")
    if env:
        conda = host.get("conda")
        if not conda:
            raise ConfigError(f"host {name} declares no conda root for --env")
        setup.append(f". {shlex.quote(conda)}/etc/profile.d/conda.sh")
        setup.append(f"conda activate {shlex.quote(env)}")
    if args.gpus:
        setup.append(f"export CUDA_VISIBLE_DEVICES={shlex.quote(args.gpus)}")
    preamble = " && ".join(setup)
    body = f"{preamble} && {rendered}" if preamble else rendered

    # The log and exit code live under the shared run root, so any host that
    # mounts it can follow the run without touching the machine running it.
    inner = (f"mkdir -p {shlex.quote(str(run_dir))} && "
             f"cd {shlex.quote(str(run_dir))} && "
             f"{{ {body} ; }} > log 2>&1; echo $? > exit_code")
    launch = (
        f"mkdir -p {shlex.quote(str(run_dir))} && "
        f"if command -v tmux >/dev/null 2>&1; then "
        f"tmux new-session -d -s {shlex.quote(run_id)} {shlex.quote(inner)}; "
        f"else setsid nohup bash -lc {shlex.quote(inner)} "
        f">/dev/null 2>&1 < /dev/null & fi; echo started"
    )
    if args.dry_run:
        print(f"run_id: {run_id}\nrun_dir: {run_dir}\nhost: {name}\n"
              f"would run: {rendered}\nsetup: {preamble}")
        return 0

    result = remote(host, launch)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        print(f"launch failed on {name}: {detail[:300]}", file=sys.stderr)
        return 1
    meta = {"run_id": run_id, "host": name, "command": command,
            "cwd": workdir, "env": env, "gpus": args.gpus,
            "started_at": datetime.datetime.now().isoformat(timespec="seconds")}
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
            encoding="utf-8")
    except OSError as exc:
        print(f"note: could not record run metadata: {exc}", file=sys.stderr)
    if args.json:
        print(json.dumps(meta, ensure_ascii=False, sort_keys=True))
    else:
        print(f"started {run_id} on {name}")
        print(f"  log:  {run_dir / 'log'}")
        print(f"  tail: compute-hosts tail {run_id}")
    return 0


def _run_state(config, run_id):
    run_dir = config["run_root"] / run_id
    meta = {}
    meta_path = run_dir / "meta.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
    exit_code = None
    exit_path = run_dir / "exit_code"
    if exit_path.is_file():
        try:
            exit_code = int(exit_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            exit_code = None
    return {"run_id": run_id, "dir": run_dir, "meta": meta,
            "exit_code": exit_code,
            "state": "finished" if exit_code is not None else "running"}


def cmd_runs(args):
    config = load_config()
    root = config["run_root"]
    if not root.is_dir():
        print(f"no runs yet: {root}")
        return 0
    rows = []
    for entry in sorted(root.iterdir(), reverse=True):
        if not entry.is_dir():
            continue
        state = _run_state(config, entry.name)
        if args.host and state["meta"].get("host") != args.host:
            continue
        rows.append(state)
        if len(rows) >= args.limit:
            break
    if args.json:
        print(json.dumps([{k: (str(v) if k == "dir" else v)
                           for k, v in row.items()} for row in rows],
                         ensure_ascii=False, sort_keys=True))
        return 0
    for row in rows:
        tail = (f"exit {row['exit_code']}" if row["exit_code"] is not None
                else "running")
        command = " ".join(row["meta"].get("command") or [])
        print(f"  {row['run_id']:<34} {tail:<10} {command[:60]}")
    if not rows:
        print("  (none)")
    return 0


def cmd_tail(args):
    config = load_config()
    state = _run_state(config, args.run_id)
    log = state["dir"] / "log"
    if not log.is_file():
        print(f"no log yet: {log}", file=sys.stderr)
        return 1
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-args.lines:]:
        print(line)
    if state["exit_code"] is not None:
        print(f"-- finished, exit {state['exit_code']} --")
    return 0


def cmd_stop(args):
    config = load_config()
    state = _run_state(config, args.run_id)
    host_name = state["meta"].get("host")
    if not host_name:
        print(f"unknown host for {args.run_id}", file=sys.stderr)
        return 1
    (name, host), = _select(config, [host_name])
    result = remote(host, f"tmux kill-session -t {shlex.quote(args.run_id)} "
                          f"2>/dev/null && echo stopped || echo 'not running'")
    print((result.stdout or result.stderr).strip())
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="compute-hosts", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="Inventory with live reachability and CPU/GPU state")
    p_list.add_argument("hosts", nargs="*")
    p_list.add_argument("--static", action="store_true", help="Skip the live probe")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_probe = sub.add_parser(
        "probe", help="Measure reachability, CPU/GPUs, and process owners")
    p_probe.add_argument("hosts", nargs="*")
    p_probe.add_argument("--json", action="store_true")
    p_probe.set_defaults(func=cmd_probe)

    p_claim = sub.add_parser(
        "claim", help="Bind a detached root PID to its exact launcher session")
    p_claim.add_argument("host")
    p_claim.add_argument("pid", type=int)
    p_claim.add_argument("--harness", required=True,
                         choices=("claude", "codex", "opencode"))
    p_claim.add_argument("--session", required=True)
    p_claim.add_argument("--json", action="store_true")
    p_claim.set_defaults(func=cmd_claim)

    p_run = sub.add_parser("run", help="Start a detached command on a host")
    p_run.add_argument("host")
    p_run.add_argument("--env", help="conda environment to activate")
    p_run.add_argument("--cwd", help="working directory on that host")
    p_run.add_argument("--gpus", help="value for CUDA_VISIBLE_DEVICES")
    p_run.add_argument("--name", help="label appended to the run id")
    p_run.add_argument("--dry-run", action="store_true")
    p_run.add_argument("--json", action="store_true")
    p_run.add_argument("command", nargs="*")
    p_run.set_defaults(func=cmd_run)

    p_runs = sub.add_parser("runs", help="List recent runs and their state")
    p_runs.add_argument("--host")
    p_runs.add_argument("--limit", type=int, default=20)
    p_runs.add_argument("--json", action="store_true")
    p_runs.set_defaults(func=cmd_runs)

    p_tail = sub.add_parser("tail", help="Show the end of a run's log")
    p_tail.add_argument("run_id")
    p_tail.add_argument("--lines", type=int, default=40)
    p_tail.set_defaults(func=cmd_tail)

    p_stop = sub.add_parser("stop", help="Stop a detached run")
    p_stop.add_argument("run_id")
    p_stop.set_defaults(func=cmd_stop)
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Everything after the first bare `--` is the command to run verbatim.
    # argparse.REMAINDER would capture this utility's own options too, so the
    # split happens before parsing rather than after.
    trailing = []
    if "--" in argv:
        cut = argv.index("--")
        argv, trailing = argv[:cut], argv[cut + 1:]
    args = build_parser().parse_args(argv)
    if getattr(args, "command", None) is not None:
        args.command = list(args.command) + trailing
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"compute-hosts: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
