#!/usr/bin/env python3
"""Run post-publish-smoke.sh's hook-file check against the shipped Claude settings."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SMOKE = HERE / "post-publish-smoke.sh"
START = """HOME_DIR="$SMOKE/home" python3 - <<'PY'"""


def hook_file_check() -> str:
    lines = SMOKE.read_text().splitlines()
    start = lines.index(START) + 1
    return "\n".join(lines[start:lines.index("PY", start)]) + "\n"


class HookFileCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        claude = self.home / ".claude"
        claude.mkdir(parents=True)
        shutil.copy2(REPO / "adapters/claude/settings.json", claude / "settings.json")
        (claude / "tools").symlink_to(REPO / "tools")
        self.hooks = claude / "hooks"

    def check(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-"], input=hook_file_check(), text=True, capture_output=True,
            env=dict(os.environ, HOME_DIR=str(self.home)), timeout=60,
        )

    def test_config_dir_default_spelling_names_the_installed_hook(self):
        self.hooks.symlink_to(REPO / "hooks")
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_missing_hook_file_still_fails(self):
        self.hooks.mkdir()
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("settings.json references missing files: ['$HOME/.claude/hooks/", result.stderr)


if __name__ == "__main__":
    unittest.main()
