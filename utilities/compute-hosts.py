#!/usr/bin/env python3
"""Inventory, probe, and run work on the operator's own compute hosts.

Sessions run on one machine while training and evaluation belong on whichever
host has the right GPUs. Without a recorded inventory every session rediscovers
addresses, ports, and environments by scanning, and every long run invents its
own answer to "where do the logs go" and "how do I get back to it".

This holds the static half in one user-owned file and measures the rest:

    list    static inventory plus live reachability and free GPU memory
    probe   the live measurement alone, for one host or all of them
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
import datetime
import importlib.util
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

SCHEMA_VERSION = 1
CONNECT_TIMEOUT = 8
PROBE_TIMEOUT = 40
LOCAL = "local"


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


def ssh_prefix(host):
    """Argv prefix that runs a command on this host, locally or over SSH."""
    if host.get("ssh_host") == LOCAL:
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
echo "host=$(hostname)"
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=index,name,memory.total,memory.used --format=csv,noheader \
    | sed 's/^/gpu=/'
else
  echo "gpu=none"
fi
echo "load=$(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null)"
"""


def probe_host(name, host):
    result = remote(host, PROBE_SCRIPT)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        return {"host": name, "reachable": False,
                "detail": (detail[-1][:120] if detail else "unreachable"),
                "gpus": []}
    gpus, hostname, load = [], None, None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("gpu=") and line != "gpu=none":
            parts = [p.strip() for p in line[4:].split(",")]
            if len(parts) == 4:
                try:
                    total = int(parts[2].split()[0])
                    used = int(parts[3].split()[0])
                except (ValueError, IndexError):
                    continue
                gpus.append({"index": int(parts[0]), "name": parts[1],
                             "total_mib": total, "used_mib": used,
                             "free_mib": total - used})
        elif line.startswith("host="):
            hostname = line[5:]
        elif line.startswith("load="):
            load = line[5:]
    return {"host": name, "reachable": True, "hostname": hostname,
            "load": load, "gpus": gpus}


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
    rows = []
    for name, host in _select(config, args.hosts):
        row = {"host": name, "ssh": host.get("ssh_host"),
               "port": host.get("ssh_port"), "conda": host.get("conda"),
               "note": host.get("note")}
        row.update(probe_host(name, host) if not args.static
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
            print(f"  {row['host']:<10} {row['ssh']}  {row.get('note') or ''}")
            continue
        summary = ", ".join(
            f"{g['index']}:{g['name'].replace('NVIDIA ', '')} "
            f"{g['free_mib'] // 1024}G free" for g in row["gpus"]) or "no gpu"
        print(f"  {row['host']:<10} up   load {row.get('load', '?')}  | {summary}")
    return 0


def cmd_probe(args):
    config = load_config()
    results = [probe_host(name, host) for name, host in _select(config, args.hosts)]
    if args.json:
        print(json.dumps(results, ensure_ascii=False, sort_keys=True))
    else:
        for row in results:
            state = "up" if row["reachable"] else f"down ({row.get('detail', '')})"
            print(f"{row['host']:<10} {state}")
            for gpu in row.get("gpus", []):
                print(f"    gpu{gpu['index']} {gpu['name']}: "
                      f"{gpu['free_mib']}/{gpu['total_mib']} MiB free")
    return 0 if all(r["reachable"] for r in results) else 1


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

    setup = []
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
              f"would run: {body}")
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

    p_list = sub.add_parser("list", help="Inventory with live reachability and GPUs")
    p_list.add_argument("hosts", nargs="*")
    p_list.add_argument("--static", action="store_true", help="Skip the live probe")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_probe = sub.add_parser("probe", help="Measure reachability and free GPU memory")
    p_probe.add_argument("hosts", nargs="*")
    p_probe.add_argument("--json", action="store_true")
    p_probe.set_defaults(func=cmd_probe)

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
