#!/usr/bin/env python3
"""One hermetic environment contract for every installer fixture entrypoint."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import shlex
from typing import Iterator, Mapping
from unittest import mock


_SCRUB = {
    "HOME",
    "ZDOTDIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    "XDG_RUNTIME_DIR",
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
    "AGENT_HOME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "HARNESS_BIN_DIR",
    "HARNESS_STATE_ROOT",
    "AGENT_DISPATCH_JOBS",
    "AGENT_ARTIFACT_ROOT",
    "AGENT_NOTES_ROOT",
    "REPORT_BUNDLE_ROOT",
    "MEM_STORE",
    "MEM_RECALL_RECEIPTS",
    "HEARTING_SAFE_LOCK_ROOT",
    "HEARTING_FIXTURE_ROOT",
}
_GIT_CONFIG_KEYS = {"GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"}


def build_environment(
    fixture_root: Path | str,
    agent_home: Path | str,
    *,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    root = Path(fixture_root).expanduser().resolve()
    source = Path(agent_home).expanduser().resolve()
    env = dict(os.environ if base is None else base)
    for key in _SCRUB:
        env.pop(key, None)
    for key in tuple(env):
        if key == "GIT_CONFIG_COUNT" or key.startswith(
            ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
        ):
            env.pop(key, None)

    home = root / "home"
    config = home / ".config"
    data = home / ".local" / "share"
    state = home / ".local" / "state"
    cache = home / ".cache"
    runtime = root / "runtime"
    # Keep TMP equal to the caller's mktemp root.  Shell fixtures conventionally
    # retain that value in a variable named TMP, so exporting a nested path would
    # silently redirect their cleanup and assertions halfway through setup.
    temp = root
    codex_home = home / ".codex"
    values = {
        "HOME": str(home),
        "ZDOTDIR": str(home),
        "XDG_CONFIG_HOME": str(config),
        "XDG_DATA_HOME": str(data),
        "XDG_STATE_HOME": str(state),
        "XDG_CACHE_HOME": str(cache),
        "XDG_RUNTIME_DIR": str(runtime),
        "CODEX_HOME": str(codex_home),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "AGENT_HOME": str(source),
        "TMPDIR": str(temp),
        "TMP": str(temp),
        "TEMP": str(temp),
        "HARNESS_BIN_DIR": str(codex_home / ".harness" / "bin"),
        "HARNESS_STATE_ROOT": str(state / "hearting"),
        "AGENT_DISPATCH_JOBS": str(state / "hearting" / "dispatch" / "jobs.log"),
        "AGENT_ARTIFACT_ROOT": str(root / "artifacts"),
        "AGENT_NOTES_ROOT": str(root / "agent-notes"),
        "REPORT_BUNDLE_ROOT": str(root / "report-bundles"),
        "MEM_STORE": str(state / "agent-memory"),
        "MEM_RECALL_RECEIPTS": str(state / "agent-memory" / "recall-opportunities"),
        "HEARTING_FIXTURE_ROOT": str(root),
        # A private HOME intentionally cannot borrow the operator's global Git
        # safe.directory list.  Trust only the explicit read-only source root
        # selected for this fixture, including NAS/worktree ownership setups.
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": str(source),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    env.update(values)
    return env


def prepare_environment(env: Mapping[str, str]) -> None:
    for key in (
        "HOME",
        "XDG_RUNTIME_DIR",
        "TMPDIR",
    ):
        path = Path(env[key])
        path.mkdir(parents=True, exist_ok=True)
    Path(env["XDG_RUNTIME_DIR"]).chmod(0o700)


def shell_exports(env: Mapping[str, str]) -> str:
    keys = sorted(_SCRUB | _GIT_CONFIG_KEYS | {"PYTHONDONTWRITEBYTECODE"})
    return "\n".join(
        f"export {key}={shlex.quote(env[key])}" for key in keys if key in env
    )


@contextmanager
def patched_environment(
    fixture_root: Path | str,
    agent_home: Path | str,
    *,
    base: Mapping[str, str] | None = None,
) -> Iterator[dict[str, str]]:
    env = build_environment(fixture_root, agent_home, base=base)
    prepare_environment(env)
    with mock.patch.dict(os.environ, env, clear=True):
        yield env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("shell", "json"))
    parser.add_argument("fixture_root")
    parser.add_argument("agent_home")
    args = parser.parse_args(argv)
    env = build_environment(args.fixture_root, args.agent_home)
    prepare_environment(env)
    if args.command == "shell":
        print(shell_exports(env))
    else:
        import json

        print(json.dumps(env, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
