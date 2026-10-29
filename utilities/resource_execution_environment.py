"""Shared, secret-free execution-environment identity for smoke and payloads."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
import shlex
import shutil

IDENTITY_ENVIRONMENT_KEYS = (
    "PATH", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CONDA_PREFIX",
    "LD_LIBRARY_PATH",
)


def _unwrap(argv):
    tokens = list(argv)
    if not tokens or not all(isinstance(token, str) for token in tokens):
        return None
    if Path(tokens[0]).name not in {"bash", "sh"}:
        return tokens
    try:
        command_index = tokens.index("-c", 1)
        script = tokens[command_index + 1]
        lexer = shlex.shlex(script, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        parsed = list(lexer)
        if not parsed or any(token and all(char in ";&|<>" for char in token)
                             for token in parsed):
            return None
        if parsed[0] == "exec":
            parsed = parsed[1:]
        # A shell expansion before a parsed command's -c payload can change
        # environment assignments without changing argv provenance.
        code_index = next((i for i, token in enumerate(parsed)
                           if token == "-c" and i > 0), len(parsed))
        if any("$" in token for token in parsed[:code_index]):
            return None
        if any(Path(token).name in {"bash", "sh"} for token in parsed[:code_index]):
            return None
        tokens = parsed
    except (ValueError, IndexError):
        return None
    if tokens and tokens[0] == "exec":
        tokens = tokens[1:]
    return tokens or None


def _parse_env(argv):
    tokens = _unwrap(argv)
    if not tokens:
        return None
    descriptor = {"inherit": True, "unset": [], "set": {}}
    if Path(tokens[0]).name != "env":
        return descriptor, tokens
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            break
        if token in {"-i", "--ignore-environment"}:
            descriptor["inherit"] = False
            index += 1
            continue
        if token in {"-u", "--unset"}:
            if index + 1 >= len(tokens):
                return None
            descriptor["unset"].append(tokens[index + 1])
            index += 2
            continue
        if token.startswith("--unset="):
            name = token.split("=", 1)[1]
            if not name:
                return None
            descriptor["unset"].append(name)
            index += 1
            continue
        if "=" in token:
            name, value = token.split("=", 1)
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                return None
            descriptor["set"][name] = value
            index += 1
            continue
        if token.startswith("-"):
            return None
        break
    command = tokens[index:]
    if not command:
        return None
    descriptor["unset"] = sorted(set(descriptor["unset"]))
    return descriptor, command


def payload_environment(argv):
    """Compatibility projection of env assignments used by resource operations."""
    parsed = _parse_env(argv)
    if parsed is None:
        return None
    return parsed[0]


def _compute_hosts(argv):
    if not argv or Path(argv[0]).name not in {"compute-hosts", "compute-hosts.py"}:
        return None
    path = Path(__file__).with_name("compute-hosts.py")
    if path.is_symlink() or not path.is_file():
        return False
    spec = importlib.util.spec_from_file_location("resource_environment_compute_hosts", path)
    if spec is None or spec.loader is None:
        return False
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        args = module.parse_run_invocation(argv)
    except (SystemExit, TypeError, ValueError):
        return False
    values = (getattr(args, "host", None), getattr(args, "env", None),
              getattr(args, "cwd", None), getattr(args, "command", None))
    if (not isinstance(values[0], str) or not values[0]
            or not isinstance(values[1], str) or not values[1]
            or not isinstance(values[2], str) or not Path(values[2]).is_absolute()
            or not isinstance(values[3], list) or not values[3]
            or not all(isinstance(item, str) for item in values[3])):
        return False
    return {"host": values[0], "env": values[1], "cwd": values[2],
            "command": values[3]}


def _environment_values(descriptor, inherited, *, remote):
    effective = dict(inherited) if descriptor["inherit"] and not remote else {}
    for name in descriptor["unset"]:
        effective.pop(name, None)
    effective.update(descriptor["set"])
    selected = {}
    for name in IDENTITY_ENVIRONMENT_KEYS:
        if name in descriptor["set"]:
            selected[name] = {"state": "set", "value": descriptor["set"][name]}
        elif name in descriptor["unset"] or not descriptor["inherit"]:
            selected[name] = {"state": "unset"}
        elif remote:
            selected[name] = {"state": "inherit"}
        elif name in effective:
            selected[name] = {"state": "inherit", "value": effective[name]}
        else:
            selected[name] = {"state": "unset"}
    return effective, selected


def _interpreter(command, environment):
    if not command:
        return None
    executable = command[0]
    path = Path(executable)
    if path.is_absolute() or "/" in executable:
        try:
            return str(path.expanduser().resolve(strict=True))
        except (OSError, RuntimeError, ValueError):
            return None
    found = shutil.which(executable, path=environment.get("PATH"))
    if not found:
        return None
    try:
        return str(Path(found).resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return None


def execution_identity(argv, cwd, *, environ=None, remote=None):
    """Return normalized execution selectors, never the full ambient environment.

    A compute-hosts selector keeps remote host/environment identity separate
    from the local launcher interpreter. Unsupported shell/env syntax returns
    None so callers refuse an unbound launch.
    """
    inherited = dict(os.environ if environ is None else environ)
    if remote is None:
        outer = _parse_env(argv)
        if outer is None:
            return None
        outer_descriptor, command = outer
        remote_probe = _compute_hosts(command)
        if remote_probe is False:
            return None
        if remote_probe is not None:
            remote = remote_probe
            parsed = _parse_env(remote["command"])
        else:
            parsed = outer
        if parsed is None:
            return None
        descriptor, payload = parsed
    else:
        parsed = _parse_env(argv)
        if parsed is None:
            return None
        descriptor, payload = parsed
    if remote is not None:
        try:
            remote_cwd = str(Path(remote["cwd"]).expanduser().resolve(strict=False))
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return None
        _effective, selected = _environment_values(descriptor, {}, remote=True)
        return {"schema_version": 1, "kind": "compute-hosts",
                "compute_host": remote.get("host"), "compute_env": remote.get("env"),
                "cwd": remote_cwd, "interpreter": payload[0],
                "environment": selected}
    try:
        resolved_cwd = str(Path(cwd).expanduser().resolve(strict=True))
    except (OSError, RuntimeError, ValueError, TypeError):
        return None
    effective, selected = _environment_values(descriptor, inherited, remote=False)
    interpreter = _interpreter(payload, effective)
    if not interpreter:
        return None
    return {"schema_version": 1, "kind": "local", "cwd": resolved_cwd,
            "interpreter": interpreter, "environment": selected}


def valid_execution_identity(value):
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return False
    if value.get("kind") not in {"local", "compute-hosts"}:
        return False
    if not all(isinstance(value.get(key), str) and value.get(key)
               for key in ("cwd", "interpreter")):
        return False
    if value["kind"] == "compute-hosts" and not all(
            isinstance(value.get(key), str) and value.get(key)
            for key in ("compute_host", "compute_env")):
        return False
    environment = value.get("environment")
    if not isinstance(environment, dict) or set(environment) != set(IDENTITY_ENVIRONMENT_KEYS):
        return False
    for state in environment.values():
        if (not isinstance(state, dict) or state.get("state") not in {"set", "unset", "inherit"}
                or (state.get("state") == "set" and not isinstance(state.get("value"), str))
                or (state.get("state") == "unset" and "value" in state)
                or (state.get("state") == "inherit" and "value" in state
                    and not isinstance(state["value"], str))):
            return False
    return True
