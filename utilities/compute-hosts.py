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
import ipaddress
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
SSH_BRIDGE_MAX_PROCESSES = 8192
SSH_BRIDGE_MAX_SOCKET_ROWS = 32768
SSH_BRIDGE_MAX_FDS = 256
SSH_BRIDGE_MAX_CONNECTIONS = 256
SSH_BRIDGE_ENV_BYTES = 1024 * 1024
SESSION_ENV_KEYS = (
    ("CLAUDE_CODE_SESSION_ID", "claude"),
    ("CODEX_THREAD_ID", "codex"),
    ("CODEX_SESSION_ID", "codex"),
    ("OPENCODE_SESSION_ID", "opencode"),
)


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
    """Inventory cannot be used. ``status`` names why: missing, template, invalid."""

    def __init__(self, message, status="invalid"):
        super().__init__(message)
        self.status = status


def load_config(path=None):
    path = Path(path) if path is not None else config_path()
    if path.is_symlink() or not path.is_file():
        raise ConfigError(
            f"compute host inventory is not initialized: {path} "
            "(`harness install` seeds a commented template there)", status="missing")
    try:
        data = _parser_module().parse_yaml_subset(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ConfigError(f"invalid compute host inventory: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise ConfigError(f"unsupported inventory schema: {path}")
    hosts = data.get("hosts")
    if not isinstance(hosts, dict) or not hosts:
        # The seeded template parses to exactly this: a schema line and an empty
        # `hosts:` mapping with every example entry still commented out.
        raise ConfigError(
            f"compute host inventory has no hosts yet: edit {path} "
            "(uncomment and fill in the host entries, then set run_root)",
            status="template")
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


def _normalize_ip(value):
    """Return one comparison form for IPv4, IPv6, and IPv4-mapped IPv6."""
    try:
        parsed = ipaddress.ip_address(str(value).split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
        parsed = parsed.ipv4_mapped
    return parsed.compressed


def _decode_proc_net_endpoint(value, family):
    """Decode one Linux /proc/net/tcp{,6} address without invoking `ss`."""
    try:
        address_hex, port_hex = value.split(":", 1)
        raw = bytes.fromhex(address_hex)
        port = int(port_hex, 16)
        if family == socket.AF_INET and len(raw) == 4:
            raw = raw[::-1]
        elif family == socket.AF_INET6 and len(raw) == 16:
            # procfs prints each native-endian 32-bit word independently.
            raw = b"".join(raw[index:index + 4][::-1]
                           for index in range(0, len(raw), 4))
        else:
            return None
        address = _normalize_ip(socket.inet_ntop(family, raw))
    except (OSError, ValueError):
        return None
    if address is None or not 0 < port <= 65535:
        return None
    return address, port


def _established_tcp_sockets(proc_root=Path("/proc")):
    """Map bounded established socket inodes to normalized endpoint tuples."""
    sockets = {}
    examined = 0
    for name, family in (("tcp", socket.AF_INET), ("tcp6", socket.AF_INET6)):
        try:
            lines = (Path(proc_root) / "net" / name).read_text(
                encoding="ascii", errors="replace").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            examined += 1
            if examined > SSH_BRIDGE_MAX_SOCKET_ROWS:
                return sockets
            fields = line.split()
            if len(fields) < 10 or fields[3] != "01":  # TCP_ESTABLISHED
                continue
            local = _decode_proc_net_endpoint(fields[1], family)
            remote_endpoint = _decode_proc_net_endpoint(fields[2], family)
            try:
                inode = int(fields[9])
            except ValueError:
                continue
            if inode > 0 and local is not None and remote_endpoint is not None:
                sockets.setdefault(inode, (local[0], local[1],
                                           remote_endpoint[0], remote_endpoint[1]))
    return sockets


def _unix_listener_inodes(proc_root=Path("/proc")):
    """Return Unix stream listeners, including OpenSSH ControlMaster sockets."""
    try:
        lines = (Path(proc_root) / "net" / "unix").read_text(
            encoding="ascii", errors="replace").splitlines()[1:]
    except OSError:
        return None
    if len(lines) > SSH_BRIDGE_MAX_SOCKET_ROWS:
        return None
    listeners = set()
    for line in lines:
        fields = line.split()
        if len(fields) < 7:
            continue
        try:
            flags = int(fields[3], 16)
            socket_type = int(fields[4], 16)
            inode = int(fields[6])
        except ValueError:
            continue
        # SO_ACCEPTCON plus SOCK_STREAM is the durable shape used by an SSH
        # multiplexing master. Excluding any such ssh-owned socket is safer
        # than assigning all multiplexed channels to the master's session.
        if flags & 0x00010000 and socket_type == 1 and inode > 0:
            listeners.add(inode)
    return listeners


def _local_proc_identity(pid, proc_root=Path("/proc")):
    """Read the stable process start time only for this effective user."""
    root = Path(proc_root) / str(pid)
    try:
        text = (root / "stat").read_text(encoding="utf-8", errors="replace")
        rest = text[text.rfind(")") + 2:].split()
        start = int(rest[19])
        status = (root / "status").read_text(encoding="utf-8", errors="replace")
        effective = None
        for line in status.splitlines():
            if line.startswith("Uid:"):
                fields = line.split()
                effective = int(fields[2]) if len(fields) >= 3 else None
                break
    except (OSError, ValueError, IndexError):
        return None
    return start if effective == os.geteuid() else None


def _is_ssh_process(pid, proc_root=Path("/proc")):
    root = Path(proc_root) / str(pid)
    try:
        if (root / "comm").read_text(encoding="utf-8", errors="replace").strip() == "ssh":
            return True
    except OSError:
        pass
    try:
        argv0 = (root / "cmdline").read_bytes()[:4096].split(b"\0", 1)[0]
    except OSError:
        return False
    return os.path.basename(argv0.decode("utf-8", errors="ignore")) == "ssh"


def _local_identity_env(pid, proc_root=Path("/proc")):
    values = {}
    allowlist = {key for key, _harness in SESSION_ENV_KEYS}
    try:
        with (Path(proc_root) / str(pid) / "environ").open("rb") as handle:
            raw = handle.read(SSH_BRIDGE_ENV_BYTES + 1)
    except OSError:
        return values
    if len(raw) > SSH_BRIDGE_ENV_BYTES:
        return values
    for item in raw.split(b"\0"):
        key, separator, value = item.partition(b"=")
        if not separator:
            continue
        decoded_key = key.decode("utf-8", errors="ignore")
        if decoded_key in allowlist:
            values[decoded_key] = value.decode("utf-8", errors="replace")
    return values


def _unique_session_owner(values):
    candidates = set()
    for key, harness in SESSION_ENV_KEYS:
        session_id = values.get(key)
        if not isinstance(session_id, str):
            continue
        if (not session_id or len(session_id) > 256 or session_id != session_id.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in session_id)):
            return None
        candidates.add((harness, session_id))
    if len(candidates) != 1:
        return None
    harness, session_id = next(iter(candidates))
    return {"kind": "session", "harness": harness, "id": session_id}


def _socket_inodes(pid, proc_root=Path("/proc")):
    inodes = set()
    try:
        entries = sorted((Path(proc_root) / str(pid) / "fd").iterdir(),
                         key=lambda entry: int(entry.name) if entry.name.isdigit() else 1 << 30)
    except OSError:
        return None
    if len(entries) > SSH_BRIDGE_MAX_FDS:
        return None
    for entry in entries:
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        if target.startswith("socket:[") and target.endswith("]"):
            try:
                inodes.add(int(target[8:-1]))
            except ValueError:
                continue
    return inodes


def _ssh_session_bridges_for_pid(pid, sockets, unix_listener_inodes,
                                 proc_root=Path("/proc")):
    before = _local_proc_identity(pid, proc_root)
    if before is None or not _is_ssh_process(pid, proc_root):
        return []
    owner = _unique_session_owner(_local_identity_env(pid, proc_root))
    if owner is None:
        return []
    inodes = _socket_inodes(pid, proc_root)
    if inodes is None or inodes & unix_listener_inodes:
        return []
    after = _local_proc_identity(pid, proc_root)
    if before != after or not _is_ssh_process(pid, proc_root):
        return []
    rows = []
    for inode in sorted(inodes):
        connection = sockets.get(inode)
        if connection is None:
            continue
        rows.append({
            "_pid": pid,
            "_proc_start": before,
            "_socket_inode": inode,
            "client_address": connection[0], "client_port": connection[1],
            "server_address": connection[2], "server_port": connection[3],
            "owner": dict(owner),
        })
    return rows


def _deduplicate_ssh_session_bridges(rows):
    """Keep one owner per tuple; a conflicting tuple is omitted, never truncated."""
    grouped = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("owner"), dict):
            continue
        key = (row.get("client_address"), row.get("client_port"),
               row.get("server_address"), row.get("server_port"))
        owner_key = (row["owner"].get("harness"), row["owner"].get("id"))
        grouped.setdefault(key, {})[owner_key] = row
    result = []
    for key in sorted(grouped, key=lambda item: tuple(map(str, item))):
        owners = grouped[key]
        if len(owners) != 1:
            continue
        row = next(iter(owners.values()))
        result.append({
            "client_address": row["client_address"],
            "client_port": row["client_port"],
            "server_address": row["server_address"],
            "server_port": row["server_port"],
            "owner": dict(row["owner"]),
        })
        if len(result) >= SSH_BRIDGE_MAX_CONNECTIONS:
            break
    return result


def collect_ssh_session_bridges(proc_root=Path("/proc")):
    """Collect transient exact session evidence for live direct SSH transports."""
    sockets_before = _established_tcp_sockets(proc_root)
    unix_listeners_before = _unix_listener_inodes(proc_root)
    if not sockets_before or unix_listeners_before is None:
        return []
    try:
        pids = sorted(int(entry.name) for entry in Path(proc_root).iterdir()
                      if entry.name.isdigit())[:SSH_BRIDGE_MAX_PROCESSES]
    except OSError:
        return []
    rows = []
    for pid in pids:
        rows.extend(_ssh_session_bridges_for_pid(
            pid, sockets_before, unix_listeners_before, proc_root))
    sockets_after = _established_tcp_sockets(proc_root)
    unix_listeners_after = _unix_listener_inodes(proc_root)
    if unix_listeners_after is None:
        return []
    stable = []
    for row in rows:
        pid = row.get("_pid")
        inode = row.get("_socket_inode")
        connection = (row.get("client_address"), row.get("client_port"),
                      row.get("server_address"), row.get("server_port"))
        if sockets_after.get(inode) != connection:
            continue
        if (_local_proc_identity(pid, proc_root) != row.get("_proc_start")
                or not _is_ssh_process(pid, proc_root)):
            continue
        current_inodes = _socket_inodes(pid, proc_root)
        if (current_inodes is None or inode not in current_inodes
                or current_inodes & unix_listeners_after):
            continue
        stable.append(row)
    return _deduplicate_ssh_session_bridges(stable)


PROBE_SCRIPT = r"""
python3 - <<'PY'
import csv
import hashlib
import ipaddress
import io
import json
import os
import socket
import subprocess
import time
import unicodedata
from pathlib import Path

try:
    OWNER_CLAIMS = json.loads(os.environ.get("HEARTING_OWNER_CLAIMS_JSON", "[]"))
except (TypeError, ValueError):
    OWNER_CLAIMS = []
if not isinstance(OWNER_CLAIMS, list):
    OWNER_CLAIMS = []
try:
    SSH_SESSION_BRIDGE_ROWS = json.loads(
        os.environ.get("HEARTING_SSH_SESSION_BRIDGES_JSON", "[]"))
except (TypeError, ValueError):
    SSH_SESSION_BRIDGE_ROWS = []
if (not isinstance(SSH_SESSION_BRIDGE_ROWS, list)
        or len(SSH_SESSION_BRIDGE_ROWS) > 256):
    SSH_SESSION_BRIDGE_ROWS = []


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


def normalized_ip(value):
    try:
        parsed = ipaddress.ip_address(str(value).split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
        parsed = parsed.ipv4_mapped
    return parsed.compressed


def normalized_ssh_connection(values):
    if isinstance(values, str):
        values = values.split()
    if not isinstance(values, (list, tuple)) or len(values) != 4:
        return None
    client_address = normalized_ip(values[0])
    server_address = normalized_ip(values[2])

    def port(value):
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            try:
                return int(value)
            except ValueError:
                return None
        return None

    client_port = port(values[1])
    server_port = port(values[3])
    if (client_address is None or server_address is None
            or client_port is None or server_port is None
            or not 0 < client_port <= 65535 or not 0 < server_port <= 65535):
        return None
    return client_address, client_port, server_address, server_port


def load_ssh_session_bridges(rows):
    bridges = {}
    if not isinstance(rows, list) or len(rows) > 256:
        return bridges
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = normalized_ssh_connection((
            row.get("client_address"), row.get("client_port"),
            row.get("server_address"), row.get("server_port"),
        ))
        owner = row.get("owner")
        if key is None or not isinstance(owner, dict) or owner.get("kind") != "session":
            continue
        harness = owner.get("harness")
        session_id = owner.get("id")
        if (harness not in {"claude", "codex", "opencode"}
                or not isinstance(session_id, str) or not session_id
                or len(session_id) > 256 or session_id != session_id.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in session_id)):
            continue
        bridges.setdefault(key, {})[(harness, session_id)] = {
            "kind": "session", "harness": harness, "id": session_id,
        }
    return bridges


SSH_SESSION_BRIDGES = load_ssh_session_bridges(SSH_SESSION_BRIDGE_ROWS)


def ssh_connection_owners(value):
    key = normalized_ssh_connection(value)
    if key is None:
        return []
    return list(SSH_SESSION_BRIDGES.get(key, {}).values())


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


def cpu_sample(cpu_count=None):
    def read():
        rows = {}
        try:
            lines = Path("/proc/stat").read_text().splitlines()
        except (OSError, ValueError, IndexError):
            return None
        for line in lines:
            fields = line.split()
            if not fields:
                continue
            label = fields[0]
            if label != "cpu" and not (label.startswith("cpu") and label[3:].isdigit()):
                continue
            try:
                values = [int(value) for value in fields[1:]]
            except ValueError:
                continue
            if len(values) < 4:
                continue
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            rows[label] = (sum(values), idle)
        return rows or None

    def utilization(before, after):
        if before is None or after is None:
            return None
        total = after[0] - before[0]
        idle = after[1] - before[1]
        if total <= 0:
            return None
        return max(0, min(100, int(round(100.0 * (total - idle) / total))))

    before = read()
    if before is None:
        return None, None
    time.sleep(0.10)
    after = read()
    if after is None:
        return None, None

    aggregate = utilization(before.get("cpu"), after.get("cpu"))
    if isinstance(cpu_count, int) and not isinstance(cpu_count, bool) and cpu_count > 0:
        labels = ["cpu%d" % index for index in range(cpu_count)]
    else:
        labels = sorted(
            (label for label in set(before) | set(after)
             if label.startswith("cpu") and label[3:].isdigit()),
            key=lambda label: int(label[3:]),
        )
    threads = [utilization(before.get(label), after.get(label)) for label in labels]
    return aggregate, (threads or None)


def memory_sample():
    # Missing inputs remain unknown instead of becoming zero.
    values = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, separator, remainder = line.partition(":")
            if not separator:
                continue
            fields = remainder.split()
            if fields:
                try:
                    values[key] = int(fields[0])
                except ValueError:
                    continue
    except OSError:
        values = {}

    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if available is None and total is not None:
        fallback = ("MemFree", "Buffers", "Cached", "SReclaimable")
        if all(key in values for key in fallback):
            available = sum(values[key] for key in fallback)
    swap_total = values.get("SwapTotal")
    swap_free = values.get("SwapFree")

    def mib(value):
        return max(0, value) // 1024 if isinstance(value, int) else None

    return {
        "memory_total_mib": mib(total),
        "memory_used_mib": mib(max(0, total - available)
                               if total is not None and available is not None else None),
        "swap_total_mib": mib(swap_total),
        "swap_used_mib": mib(max(0, swap_total - swap_free)
                             if swap_total is not None and swap_free is not None else None),
    }


def same_euid(pid):
    try:
        for line in (Path("/proc") / str(pid) / "status").read_text().splitlines():
            if line.startswith("Uid:"):
                fields = line.split()
                return len(fields) >= 3 and int(fields[2]) == os.geteuid()
    except (OSError, ValueError):
        pass
    return False


COMMAND_BYTES_MAX = 4096
COMMAND_ARGV_MAX = 32
COMMAND_CELLS_MAX = 160


def command_text(values):
    # One control-free, display-bounded line from already bounded argv bytes.
    words = []
    for raw in list(values)[:COMMAND_ARGV_MAX]:
        if isinstance(raw, bytes):
            text = raw.decode("utf-8", errors="replace")
        else:
            text = str(raw or "")
        text = "".join(" " if unicodedata.category(char).startswith("C") else char
                       for char in text)
        text = " ".join(text.split())
        if text:
            words.append(text)
    joined = " ".join(words)
    out = []
    cells = 0
    for char in joined:
        width = (0 if unicodedata.combining(char) else
                 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1)
        if cells + width > COMMAND_CELLS_MAX:
            break
        out.append(char)
        cells += width
    return "".join(out) or None


def process_command(pid, expected_start, process_name):
    # Read cmdline only across one same-EUID, stable pid/start observation.
    fallback = command_text([process_name])
    before = proc_stat(pid)
    if (before is None or before["start"] != expected_start or not same_euid(pid)):
        return fallback
    try:
        with (Path("/proc") / str(pid) / "cmdline").open("rb") as handle:
            raw = handle.read(COMMAND_BYTES_MAX + 1)[:COMMAND_BYTES_MAX]
    except OSError:
        return fallback
    after = proc_stat(pid)
    if (after is None or after["start"] != expected_start or not same_euid(pid)):
        return fallback
    return command_text(raw.split(b"\0")) or fallback


ENV_KEYS = {
    "AGENT_DISPATCH_ATTEMPT_ID", "AGENT_DISPATCH_SELF_SLUG",
    "HEARTING_COMPUTE_RUN_ID", "HEARTING_COMPUTE_HOST",
    "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID",
    "OPENCODE_SESSION_ID", "SSH_CONNECTION",
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
        for bridge_owner in ssh_connection_owners(env.get("SSH_CONNECTION")):
            harness = bridge_owner["harness"]
            sid = bridge_owner["id"]
            candidates["session"].append({
                "kind": "session", "id": sid,
                "label": "%s:%s" % (harness, safe_label(sid[:8], "session")),
                "harness": harness, "evidence_pid": current,
                "evidence_start": stat["start"], "ancestry_depth": depth,
                "source": "ssh-connection+ancestry",
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
            return None, "ancestor-reused-or-gone", None
        current = stat["ppid"]
        depth += 1

    final_stat = proc_stat(pid)
    if final_stat is None or final_stat["start"] != expected_start:
        return None, "pid-reused-or-gone", None
    exact_sessions = {}
    for candidate in candidates["session"]:
        harness, sid = candidate.get("harness"), candidate.get("id")
        if harness not in {"claude", "codex", "opencode"} or not isinstance(sid, str) or not sid:
            continue
        key = (harness, sid)
        prior = exact_sessions.get(key)
        if prior is None or candidate["ancestry_depth"] < prior["ancestry_depth"]:
            exact_sessions[key] = candidate
    session_owner = (next(iter(exact_sessions.values()))
                     if len(exact_sessions) == 1 else None)
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
            return next(iter(unique.values())), None, session_owner
        if len(unique) > 1:
            return None, "ambiguous-" + kind, session_owner
    return None, "no-exact-owner", session_owner


cpu_count = os.cpu_count()
cpu_utilization, cpu_threads = cpu_sample(cpu_count)
if cpu_count is None and cpu_threads:
    cpu_count = len(cpu_threads)
payload = {
    "hostname": socket.gethostname(), "load": None,
    "cpu_count": cpu_count, "cpu_utilization_pct": cpu_utilization,
    "cpu_thread_utilization_pct": cpu_threads, "gpus": [],
    "unmatched_processes": [], "observed_at": time.time(),
}
payload.update(memory_sample())
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
        owner, reason, session_owner = (
            process_owner(pid, stat["start"])
            if stat is not None else (None, "process-unavailable", None)
        )
        process = {
            "gpu_uuid": uuid or None, "pid": pid,
            "proc_start": stat["start"] if stat is not None else None,
            "process_name": process_name or None, "used_memory_mib": integer(used),
            "command": process_command(pid, stat["start"], process_name)
            if stat is not None else command_text([process_name]),
            "owner": owner, "attribution_reason": reason,
        }
        if session_owner is not None:
            process["session_owner"] = session_owner
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


def probe_host(name, host, owner_claims=None, ssh_session_bridges=None):
    observed_at = datetime.datetime.now().timestamp()
    claims_json = json.dumps(list(owner_claims or ()), ensure_ascii=False,
                             separators=(",", ":"))
    if ssh_session_bridges is None:
        ssh_session_bridges = collect_ssh_session_bridges()
    bridges_json = json.dumps(list(ssh_session_bridges)[:SSH_BRIDGE_MAX_CONNECTIONS],
                              ensure_ascii=False, separators=(",", ":"))
    script = ("export HEARTING_OWNER_CLAIMS_JSON=%s\n"
              "export HEARTING_SSH_SESSION_BRIDGES_JSON=%s\n%s") % (
                  shlex.quote(claims_json), shlex.quote(bridges_json), PROBE_SCRIPT)
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
           "cpu_thread_utilization_pct": payload.get("cpu_thread_utilization_pct"),
           "memory_total_mib": payload.get("memory_total_mib"),
           "memory_used_mib": payload.get("memory_used_mib"),
           "swap_total_mib": payload.get("swap_total_mib"),
           "swap_used_mib": payload.get("swap_used_mib"),
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
    ssh_session_bridges = collect_ssh_session_bridges()
    workers = min(8, len(selected))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(
            lambda item: probe_host(item[0], item[1], claims_by_host.get(item[0], ()),
                                    ssh_session_bridges),
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
    log_path = run_dir / "log"
    exit_path = run_dir / "exit_code"
    inner = (f"mkdir -p {shlex.quote(str(run_dir))} && "
             f"cd {shlex.quote(str(run_dir))} && "
             f"( {body} ) > {shlex.quote(str(log_path))} 2>&1; "
             f"run_status=$?; printf '%s\\n' \"$run_status\" "
             f"> {shlex.quote(str(exit_path))}")
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
