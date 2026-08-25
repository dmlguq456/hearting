"""Create-once user template for the operator compute-host inventory.

The inventory names the machines an agent may probe or start detached work on.
It is user-authored: install seeds a fully commented template once so a fresh
operator sees the schema and the path, and never touches the file again.
Validation is delegated to ``utilities/compute-hosts.py``, the only reader of
the file's semantics, so this module cannot drift from it.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import paths


ENV_NAME = "COMPUTE_HOSTS_CONFIG"

TEMPLATE = """\
# Hearting compute-host inventory (user-owned).
# Seeded once by `harness install`; install, update, and uninstall never modify it.
# Keep this file byte-identical on every machine that shares the run root: the
# local machine is discovered by matching `hostname`, never written down.
#
# Until at least one host below is uncommented and filled in, `compute-hosts`
# and the Fleet COMPUTE RESOURCES panel report this file as a template.
# Check the result with:  harness config status
schema_version: 1

# Absolute directory reachable from every host (for example an NFS mount).
# Detached runs write their logs, metadata, and process-owner claims here.
# run_root: /shared/hearting/runs

hosts:
  # The key is the label used on the command line and in Fleet; it does not
  # have to match the system hostname.
  # gpu-a:
  #   ssh_host: gpu-a.example.internal   # address `ssh` connects to, or `local` for this machine
  #   ssh_user: alice                    # optional; omit to use your ssh config
  #   ssh_port: 22                       # optional
  #   hostname: gpu-a                    # output of `hostname` there; marks the entry as local when it matches
  #   workdir: /home/alice/projects      # optional default working directory for `run`
  #   conda: torch                       # optional conda environment activated before `run`
  # gpu-b:
  #   ssh_host: 10.0.0.12
  #   hostname: gpu-b
"""


def config_path() -> Path:
    override = os.environ.get(ENV_NAME)
    if override:
        return Path(override).expanduser()
    return paths.hearting_config_dir() / "compute-hosts.yaml"


def _tool_module():
    path = paths.agent_home() / "utilities" / "compute-hosts.py"
    spec = importlib.util.spec_from_file_location("_hearting_compute_hosts", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ensure(*, dry_run: bool = False) -> dict:
    path = config_path()
    if path.exists() or path.is_symlink():
        return {"status": "preserved", "path": str(path)}
    if dry_run:
        return {"status": "would-create", "path": str(path)}
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return {"status": "preserved", "path": str(path)}
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(TEMPLATE)
    return {"status": "created", "path": str(path)}


def validate() -> dict:
    """missing / template / invalid / valid; only ``invalid`` is a failure."""
    path = config_path()
    tool = _tool_module()
    try:
        config = tool.load_config(path)
    except tool.ConfigError as exc:
        status = getattr(exc, "status", "invalid")
        return {"status": status, "ok": status != "invalid", "path": str(path),
                "detail": str(exc)}
    hosts = sorted(config["hosts"])
    return {"status": "valid", "ok": True, "path": str(path),
            "detail": f"{len(hosts)} host(s): {', '.join(hosts)}",
            "hosts": hosts, "run_root": str(config["run_root"])}
