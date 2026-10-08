"""Runtime-home projection shared by registered workers and synchronous callers."""
import importlib.util
import json
import os
from pathlib import Path

_path = Path(__file__).resolve().parents[1] / 'tools/profile/build-home.py'
_spec = importlib.util.spec_from_file_location('hearting_worker_home_builder', _path)
_builder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_builder)

prepare_worker_home = _builder.build_worker_home


def codex_worker_arguments(env=None):
    env = os.environ if env is None else env
    return [item for value in json.loads(env.get('HEARTING_CODEX_WORKER_OVERRIDES', '[]'))
            for item in ('-c', value)]


def claude_worker_arguments(env=None):
    env = os.environ if env is None else env
    if not env.get('HEARTING_WORKER_HOME'):
        return []
    return ['--disable-slash-commands', '--strict-mcp-config',
            '--mcp-config', '{"mcpServers":{}}']
