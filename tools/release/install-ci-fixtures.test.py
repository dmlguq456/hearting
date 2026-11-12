#!/usr/bin/env python3
"""Exercise the fixture installer without apt, sudo, or network access."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).with_name("install-ci-fixtures.sh")


class FixtureInstallTest(unittest.TestCase):
    def run_install(self, *, installed=(), mode="fresh", packages=("ripgrep", "strace")):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            calls = root / "calls.jsonl"
            for name in ("mktemp", "rm"):
                (bin_dir / name).symlink_to(f"/usr/bin/{name}")
            for name, body in {
                "sudo": '#!/bin/sh\nexec "$@"\n',
                "timeout": '#!/bin/sh\nshift\nexec "$@"\n',
                "apt-get": f'''#!{sys.executable}
import json, os, pathlib, sys
args = sys.argv[1:]
path = pathlib.Path(os.environ["CALLS"])
prior = path.read_text().splitlines() if path.exists() else []
sources = next((a.split("=", 1)[1] for a in args if a.startswith("Dir::Etc::sourcelist=")), None)
with path.open("a") as f:
    f.write(json.dumps({{"args": args, "sources": pathlib.Path(sources).read_text() if sources else None}}) + "\\n")
mode = os.environ["MODE"]
if mode != "fresh" and not prior:
    sys.exit(124)
if mode == "update-fails" and "update" in args:
    sys.exit(100)
if mode == "install-fails" and "install" in args:
    sys.exit(100)
if "install" in args and mode != "missing-tool":
    for package, command in (("ripgrep", "rg"), ("strace", "strace"), ("bubblewrap", "bwrap")):
        if package in args:
            tool = pathlib.Path(os.environ["PATH"]) / command
            tool.write_text("#!/bin/sh\\nexit 0\\n")
            tool.chmod(0o755)
''',
            }.items():
                p = bin_dir / name
                p.write_text(body)
                p.chmod(0o755)
            for name in installed:
                p = bin_dir / name
                p.write_text("#!/bin/sh\nexit 0\n")
                p.chmod(0o755)
            result = subprocess.run(["/bin/bash", str(SCRIPT), *packages],
                env=dict(os.environ, PATH=str(bin_dir), CALLS=str(calls), MODE=mode),
                capture_output=True, text=True)
            records = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
            return result, records

    def test_existing_tools_need_no_package_operation(self):
        result, calls = self.run_install(installed=("rg", "strace"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [])

    def test_installs_only_missing_tool_without_refreshing_indexes(self):
        result, calls = self.run_install(installed=("rg",))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 1)
        self.assertIn("strace", calls[0]["args"])
        self.assertNotIn("ripgrep", calls[0]["args"])
        self.assertIsNone(calls[0]["sources"])

    def test_stalled_mirror_gets_one_official_source_retry(self):
        result, calls = self.run_install(mode="stalled")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 3)
        self.assertIn("update", calls[1]["args"])
        self.assertIn("install", calls[2]["args"])
        self.assertEqual(calls[1]["sources"], calls[2]["sources"])
        for call in calls[1:]:
            self.assertIn("Dir::Etc::sourceparts=-", call["args"])
            self.assertIn("Acquire::https::Timeout=15", call["args"])
            self.assertIn("https://archive.ubuntu.com/ubuntu", call["sources"])
            self.assertIn("https://security.ubuntu.com/ubuntu", call["sources"])
            self.assertNotIn("azure", call["sources"])
            self.assertNotIn("trusted=yes", call["sources"])

    def test_fallback_failure_cannot_hide_a_missing_fixture(self):
        for mode, count in (("update-fails", 2), ("install-fails", 3), ("missing-tool", 3)):
            with self.subTest(mode=mode):
                result, calls = self.run_install(mode=mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(len(calls), count)

    def test_namespace_fixture_uses_the_same_retry(self):
        result, calls = self.run_install(mode="stalled", packages=("bubblewrap",))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("bubblewrap", calls[-1]["args"])


if __name__ == "__main__":
    unittest.main()
