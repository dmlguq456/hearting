#!/usr/bin/env python3
"""A steward surface must address the herdr session that holds the pane.

herdr runs one server per session, each with its own socket and its own pane
namespace, so a bare `herdr ...` call only ever reaches the default server.
"""
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]
STEWARD = ROOT / "utilities/peer-steward.py"
sys.path.insert(0, str(ROOT / "utilities"))
sys.path.insert(0, str(ROOT / "tools"))
from split_module_fixture import patch_split

FAKE_HERDR = """#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["HERDR_ARGV_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\\n")
# Only the named session knows the pane.
if argv[:2] == ["--session", "fourth"] and argv[2:4] == ["agent", "get"]:
    print(json.dumps({"result": {"agent": {
        "agent": "codex",
        "agent_session": {"value": "session-in-fourth"}}}}))
    sys.exit(0)
print(json.dumps({"error": {"code": "agent_not_found"}}))
sys.exit(1)
"""


class HerdrSessionTargeting(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="peer-steward-session-")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        bindir = base / "bin"
        bindir.mkdir()
        fake = bindir / "herdr"
        fake.write_text(FAKE_HERDR)
        fake.chmod(0o755)
        self.log = base / "argv.log"
        os.environ["PATH"] = str(bindir) + os.pathsep + os.environ["PATH"]
        os.environ["HERDR_ARGV_LOG"] = str(self.log)
        self.addCleanup(os.environ.pop, "HERDR_ARGV_LOG", None)

    def load(self, session=None):
        if session is None:
            os.environ.pop("AGENT_HERDR_SESSION", None)
        else:
            os.environ["AGENT_HERDR_SESSION"] = session
        spec = importlib.util.spec_from_file_location("peer_steward_under_test", str(STEWARD))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_default_session_cannot_resolve_a_pane_of_another_session(self):
        module = self.load()
        self.assertEqual(module._resolve_target("w1:p3"), (None, None, None))
        self.assertNotIn("--session", self.log.read_text())

    def test_selected_session_resolves_and_is_passed_to_every_call(self):
        module = self.load("fourth")
        harness, sid, _ = module._resolve_target("w1:p3")
        self.assertEqual((harness, sid), ("codex", "session-in-fourth"))
        for line in self.log.read_text().splitlines():
            self.assertTrue(line.startswith('["--session", "fourth"'), line)

    def test_call_sites_added_after_the_first_fix_carry_the_session_too(self):
        # Pane/agent surfaces that were not in the first version of this test:
        # `pane get`, `pane close`, `agent prompt`, `agent read`, `agent explain`.
        module = self.load("fourth")
        module._pane_has_agent("w1:p3")
        module._close_pane("w1:p3")
        module._herdr_prompt("w1:p3", "hello", wait=False, timeout_ms=1000)
        module._form_open("w1:p3")
        module._blocking_screen_for_target("w1:p3", "codex")
        module._bottom_form_tokens("w1:p3")
        lines = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertGreaterEqual(len(lines), 5, lines)
        for argv in lines:
            self.assertEqual(argv[:2], ["--session", "fourth"], argv)
        self.assertEqual({tuple(argv[2:4]) for argv in lines},
                         {("pane", "get"), ("pane", "close"), ("agent", "prompt"),
                          ("agent", "read"), ("agent", "get")})

    def test_no_herdr_invocation_bypasses_the_session_helper(self):
        # A bare `["herdr", ...]` argv literal can only reach the default server.
        source = STEWARD.read_text(encoding="utf-8")
        self.assertEqual(re.findall(r'\[\s*"herdr"\s*,', source), [])

    def test_flag_sets_the_environment_a_detached_watcher_inherits(self):
        os.environ.pop("AGENT_HERDR_SESSION", None)
        result = subprocess.run(
            [sys.executable, str(STEWARD), "--herdr-session", "fourth", "status", "--json"],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(os.environ.pop, "AGENT_HERDR_SESSION", None)

    def test_identical_pane_ids_on_named_endpoints_get_distinct_seats(self):
        import session_tidy
        with unittest.mock.patch.dict(os.environ, {
            "AGENT_HERDR_SESSION": "left", "HERDR_SOCKET_PATH": "/tmp/herdr-left.sock"}):
            left = session_tidy._pane_seat_of("shared:p1")
        with unittest.mock.patch.dict(os.environ, {
            "AGENT_HERDR_SESSION": "right", "HERDR_SOCKET_PATH": "/tmp/herdr-right.sock"}):
            right = session_tidy._pane_seat_of("shared:p1")
        self.assertNotEqual(left.key, right.key)
        self.assertNotEqual(left.endpoint, right.endpoint)

    def test_codex_candidate_follows_replaced_sid_and_still_refuses_old_mismatch(self):
        module = self.load("fourth")
        old, new, pane = ("01a1827d-0000-7000-8000-000000000001",
                          "01a186b9-0000-7000-8000-000000000002", "w1:p3")
        info = {"pane_id": pane, "foreground_processes": [{"pid": 4242}]}

        def candidate(herdr_sid, **expected):
            ident = {"harness": "codex", "pane": pane, "session_id": herdr_sid, "name": None}
            with unittest.mock.patch.object(module, "_prompt_process_session",
                                            return_value=(new, None)):
                return module._prompt_current_candidate(
                    pane, expected_harness="codex", target_observation=("idle", ident),
                    pane_info=info, **expected)

        # after the Herdr report follows the /clear, the proven process SID is the candidate
        found, error = candidate(new)
        self.assertIsNone(error)
        self.assertEqual(found["session_id"], new)
        found, error = candidate(new, expected_sid=new)
        self.assertIsNone(error)
        self.assertEqual(found["session_id"], new)
        # a Herdr SID that is still the old one, or an expectation of it, is refused
        self.assertEqual(candidate(old), (None, "target-identity-changed"))
        self.assertEqual(candidate(new, expected_sid=old), (None, "target-identity-changed"))

    def test_opencode_clear_requires_the_current_native_selection(self):
        module = self.load("fourth")
        req = {"seat": {"pane": "w1:p3"}, "harness": "opencode", "sid": "booked-sid"}
        agent = {"pane": "w1:p3", "harness": "opencode", "session_id": "stale-herdr-sid"}
        with patch_split(module, "_interpret_payload",
                                        return_value=("idle", agent, 0, None)), \
                unittest.mock.patch.object(module, "_process_session", return_value="current-sid"):
            reason, _agent, _lines = module._clear_look("oc-live", req)
        self.assertEqual(reason, "target-changed")

        with patch_split(module, "_interpret_payload",
                                        return_value=("idle", agent, 0, None)), \
                unittest.mock.patch.object(module, "_process_session", return_value="booked-sid"), \
                unittest.mock.patch.object(module, "_read_screen", return_value=[]), \
                unittest.mock.patch.object(module, "_screen_ready", return_value=None):
            reason, _agent, _lines = module._clear_look("oc-live", req)
        self.assertIsNone(reason)


if __name__ == "__main__":
    unittest.main()
