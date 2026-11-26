#!/usr/bin/env python3
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def fingerprint(path: Path) -> tuple[bool, int, int, str]:
    if not path.exists():
        return False, 0, 0, ""
    info = path.stat()
    return (
        True,
        info.st_mtime_ns,
        info.st_size,
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )


class ExecutionAccessGlobalStateTest(unittest.TestCase):
    def test_suite_does_not_change_user_config_credentials_or_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "home"
            codex = home / ".codex"
            claude = home / ".claude"
            config = root / "config" / "hearting"
            for directory in (codex, claude, config):
                directory.mkdir(parents=True)
            guarded = (
                codex / "config.toml",
                codex / "auth.json",
                claude / "settings.json",
                claude / ".credentials.json",
                config / "dispatch-defaults.yaml",
            )
            for index, path in enumerate(guarded):
                path.write_text(f"fixture-{index}\n", encoding="utf-8")
            before = {path: fingerprint(path) for path in guarded}
            env = {
                **os.environ,
                "HOME": str(home),
                "CODEX_HOME": str(codex),
                "CLAUDE_CONFIG_DIR": str(claude),
                "XDG_CONFIG_HOME": str(root / "config"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "safe.directory",
                "GIT_CONFIG_VALUE_0": str(ROOT),
            }
            env.pop("AGENT_DISPATCH_EXECUTION_ACCESS_FILE", None)
            for script in (
                "utilities/execution_access.test.py",
                "utilities/execution_access_diagnose.test.py",
                "utilities/execution_access_builders.test.py",
            ):
                result = subprocess.run(
                    ["python3", script],
                    cwd=ROOT,
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
                self.assertEqual(0, result.returncode, f"{script}:\n{result.stdout}")
            after = {path: fingerprint(path) for path in guarded}
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
