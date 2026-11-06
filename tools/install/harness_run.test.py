#!/usr/bin/env python3
"""`hearting run <utility>`: the one PATH entry that needs no session environment."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "tools/install/harness.sh"


def run(*argv, env=None):
    base = {k: v for k, v in os.environ.items() if k != "AGENT_HOME"}
    return subprocess.run(["sh", str(LAUNCHER), "run", *argv], text=True, capture_output=True,
                          env={**base, **(env or {})}, check=False)


def launch(*argv, env=None):
    """Run the launcher without the `run` subcommand (`reports`, `open`, install...)."""
    base = {k: v for k, v in os.environ.items() if k != "AGENT_HOME"}
    return subprocess.run(["sh", str(LAUNCHER), *argv], text=True, capture_output=True,
                          env={**base, **(env or {})}, check=False)


class HarnessRunTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        (self.home / "core").mkdir()
        (self.home / "core/CORE.md").write_text("x\n")
        (self.home / "utilities").mkdir()
        (self.home / "utilities/echo-args.py").write_text(
            "import os, sys\nprint(os.environ['AGENT_HOME'], *sys.argv[1:])\n")

    def test_a_session_root_runs_its_own_utility_by_name(self):
        for name in ("echo-args", "echo-args.py"):
            with self.subTest(name=name):
                done = run(name, "--route", "r.json", env={"AGENT_HOME": str(self.home)})
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(done.stdout.split(), [str(self.home), "--route", "r.json"])

    def test_without_a_session_root_the_launcher_root_answers(self):
        done = run("agent-home")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(Path(done.stdout.strip()).resolve(), ROOT)

    def test_a_path_or_an_unknown_name_is_refused(self):
        for name in ("../core/CORE.md", ".hidden", "no-such-utility"):
            with self.subTest(name=name):
                done = run(name, env={"AGENT_HOME": str(self.home)})
                self.assertEqual(done.returncode, 2)
                self.assertIn("harness:", done.stderr)


class HarnessEntranceTest(unittest.TestCase):
    """`hearting reports` / `hearting open` go through this one launcher in every runtime."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        (self.home / "core").mkdir()
        (self.home / "core/CORE.md").write_text("x\n")
        (self.home / "utilities").mkdir()
        (self.home / "utilities/artifact_entrance.py").write_text(
            "import sys\nprint('entrance', *sys.argv[1:])\n")

    def test_reports_and_open_are_delegated_with_their_arguments(self):
        for argv in (("reports",), ("open", "3"), ("open", "beta", "result")):
            with self.subTest(argv=argv):
                done = launch(*argv, env={"AGENT_HOME": str(self.home)})
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(done.stdout.split(), ["entrance", *argv])

    def test_a_missing_entrance_module_is_a_typed_refusal(self):
        # destructive-ok: reason=remove the fixture entrance module to test the launcher's refusal; boundary=one fixture file under self.home in this test TemporaryDirectory
        (self.home / "utilities/artifact_entrance.py").unlink()
        done = launch("reports", env={"AGENT_HOME": str(self.home)})
        self.assertEqual(done.returncode, 2)
        self.assertIn("artifact entrance is missing", done.stderr)

    def test_the_real_repository_entrance_runs_against_a_temporary_root(self):
        artifact_root = Path(self._tmp.name) / "reports"
        artifact_root.mkdir()
        done = launch("reports", env={"AGENT_ARTIFACT_ROOT": str(artifact_root)})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(str(artifact_root / "README.md"), done.stdout)
        self.assertTrue((artifact_root / "README.md").is_file())


class HarnessViewerTest(HarnessEntranceTest):
    def test_optional_viewer_delegation_and_missing(self):
        viewer = self.home / 'utilities/artifact_viewer.py'
        for runtime in ('claude', 'codex', 'opencode'):
            viewer.write_text("import sys\nprint('viewer', *sys.argv[1:])\n")
            done = launch('view', 'unexpected', env={'AGENT_HOME': str(self.home), 'TEST_RUNTIME': runtime})
            self.assertEqual(done.returncode, 0)
            self.assertEqual(done.stdout.strip(), 'viewer unexpected')
        viewer.unlink()  # destructive-ok: own fixture in TemporaryDirectory
        result = launch('view', env={'AGENT_HOME': str(self.home)})
        self.assertEqual(result.returncode, 2)
        self.assertIn('optional artifact viewer is missing', result.stderr)


if __name__ == "__main__":
    unittest.main()
