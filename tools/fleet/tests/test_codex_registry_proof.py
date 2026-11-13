#!/usr/bin/env python3
"""A launcher registry entry alone never proves a daemon-attached TUI thread."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[2]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from fleet import session_registry  # noqa: E402
from fleet.collectors import codex, procscan  # noqa: E402

THREAD = "01000013-0000-7000-8000-00000000001b"


class RegistryNotIdentityTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old = os.environ.get("FLEET_SESSION_REGISTRY_DIR")
        os.environ["FLEET_SESSION_REGISTRY_DIR"] = self._tmp.name
        self.addCleanup(self._restore)
        self.proc = subprocess.Popen(["sleep", "60"], cwd=self._tmp.name)
        self.addCleanup(self.proc.wait)
        self.addCleanup(self.proc.kill)

    def _restore(self):
        if self._old is None:
            os.environ.pop("FLEET_SESSION_REGISTRY_DIR", None)
        else:
            os.environ["FLEET_SESSION_REGISTRY_DIR"] = self._old

    def _write(self, **fields):
        record = {"sessionId": THREAD, "procStart": procscan.read_proc_start(self.proc.pid),
                  "harness": "codex", "kind": "codex-tui"}
        record.update(fields)
        session_registry.write("codex", self.proc.pid, record)

    def _resolve_as_fdless_tui(self):
        with mock.patch.object(codex.os, "readlink", return_value=self._tmp.name), \
                mock.patch.object(procscan, "read_environ", return_value={"CODEX_HOME": self._tmp.name}), \
                mock.patch.object(procscan, "codex_effective_cwd", return_value=self._tmp.name), \
                mock.patch.object(procscan, "_read_argv", return_value=["codex"]), \
                mock.patch.object(procscan, "_comm_of", return_value="codex"), \
                mock.patch.object(procscan, "invocation_mode", return_value="session"), \
                mock.patch.object(procscan, "is_shared_codex_daemon", return_value=False), \
                mock.patch.object(codex, "_proc_rollout", return_value=None), \
                mock.patch.object(codex, "_daemon_session_for_process", return_value=None):
            return codex.session_id_of_process(self.proc.pid)

    def test_matching_or_recycled_registry_record_stays_unknown_without_live_proof(self):
        self._write()
        self.assertEqual(session_registry.read("codex", self.proc.pid)["sessionId"], THREAD)
        self.assertIsNone(self._resolve_as_fdless_tui())

        self._write(procStart="1")
        self.assertIsNone(self._resolve_as_fdless_tui())

    def test_missing_or_malformed_registry_data_stays_unknown(self):
        self.assertIsNone(self._resolve_as_fdless_tui())
        self._write(procStart=None)
        self.assertIsNone(self._resolve_as_fdless_tui())
        self._write(sessionId="not-a-thread")
        self.assertIsNone(self._resolve_as_fdless_tui())


if __name__ == "__main__":
    unittest.main()
