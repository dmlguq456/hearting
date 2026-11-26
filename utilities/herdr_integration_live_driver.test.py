#!/usr/bin/env python3
"""Hermetic contract tests for the bounded native integration evidence driver."""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import importlib.util
import json
import os
from pathlib import Path
import shlex
import socket
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "utilities" / "herdr_integration_live.py"
SPEC = importlib.util.spec_from_file_location("herdr_integration_live_driver", DRIVER)
live = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = live
SPEC.loader.exec_module(live)


def _native_agent(*, name="fixture-agent", runtime="codex", pane="private-pane",
                  sid="typed-native-sid", session_kind="id", session_runtime="codex"):
    agent = {"name": name, "agent": runtime, "pane_id": pane,
             "agent_session": {"source": "fixture", "agent": session_runtime,
                               "kind": session_kind}}
    if sid is not None:
        agent["agent_session"]["value"] = sid
    return agent


class NativeDriverContractTest(unittest.TestCase):
    def test_case_set_matches_the_native_measurement_matrix(self):
        self.assertEqual(set(live.CASES), {
            "callbacks", "codex-async-local-skip", "codex-busy",
            "opencode-busy", "booked-seat", "events-lost",
        })

    def test_prompt_payload_is_redacted_from_durable_argv(self):
        rendered = live._redact_argv([
            "herdr", "agent", "prompt", "private-agent",
            "do not store this prompt", "--wait", "--attach-url",
            "https://user:pass@example.test/private?token=secret",
        ])
        text = " ".join(rendered)
        self.assertNotIn("do not store", text)
        self.assertNotIn("user:pass", text)
        self.assertNotIn("token=secret", text)

    def test_codex_start_submits_one_bounded_input_and_redacts_durable_argv(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            captured_argv = root / "private-start-argv.json"
            fake_herdr = root / "herdr-fixture"
            fake_herdr.write_text(
                "#!/usr/bin/env python3\n"
                "import json, pathlib, sys\n"
                "args = sys.argv[1:]\n"
                f"capture = pathlib.Path({str(captured_argv)!r})\n"
                "if args[args.index('agent') + 1] == 'start':\n"
                "    capture.write_text(json.dumps(args))\n"
                "    print(json.dumps({'result': {'agent': {'name': 'fixture-agent', "
                "'agent_status': 'idle'}}}))\n"
                "elif args[args.index('agent') + 1] == 'get':\n"
                "    print(json.dumps({'result': {'agent': {'name': 'fixture-agent', 'agent': 'codex', "
                "'pane_id': 'private-pane', 'agent_session': {'agent': 'codex', 'kind': 'id', "
                "'value': 'typed-native-sid'}, "
                "'agent_status': 'idle'}}}))\n"
                "else:\n"
                "    print(json.dumps({'error': {'code': 'unexpected-command'}}))\n"
                "    raise SystemExit(1)\n",
                encoding="utf-8")
            fake_herdr.chmod(0o700)
            driver = live.HerdrIntegrationLive(live.build_parser().parse_args([
                "--case", "callbacks", "--output-dir", str(root / "out"),
                "--project", str(project), "--timeout", "10", "--herdr", str(fake_herdr),
            ]))
            self.addCleanup(driver.runtime_dir.cleanup)

            native = driver._agent_start("codex", "private-pane", name="fixture-agent")

            self.assertTrue(native["start_ok"])
            self.assertTrue(native["session_confirmed_by_get"])
            self.assertEqual(native["session_id"], "typed-native-sid")
            actual_argv = json.loads(captured_argv.read_text(encoding="utf-8"))
            start_at = actual_argv.index("start")
            self.assertEqual(actual_argv[start_at - 1], "agent")
            delimiter = actual_argv.index("--", start_at)
            self.assertEqual(actual_argv[delimiter + 1:], [live.NATIVE_START_PROMPT])
            self.assertEqual(actual_argv.count(live.NATIVE_START_PROMPT), 1)

            rows = [json.loads(line) for line in driver.commands_path.read_text().splitlines()]
            start_row = next(row for row in rows if row["step"] == "native-codex-session-start")
            self.assertEqual(start_row["argv"].count("[redacted]"), 1)
            self.assertNotIn(live.NATIVE_START_PROMPT, driver.commands_path.read_text())

    def test_opencode_start_submits_one_initial_prompt_and_redacts_durable_argv(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            captured_calls = root / "private-herdr-calls.json"
            fake_herdr = root / "herdr-fixture"
            fake_herdr.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys\n"
                "args = sys.argv[1:]\n"
                f"capture = pathlib.Path({str(captured_calls)!r})\n"
                "if args[args.index('agent') + 1] == 'start':\n"
                "    capture.write_text(json.dumps({'argv': args, 'cwd': os.getcwd()}))\n"
                "    print(json.dumps({'result': {'agent': {'name': 'fixture-agent', "
                "'agent_status': 'idle'}}}))\n"
                "elif args[args.index('agent') + 1] == 'get':\n"
                "    print(json.dumps({'result': {'agent': {"
                "'name': 'fixture-agent', 'agent': 'opencode', 'pane_id': 'private-pane', "
                "'agent_session': {'agent': 'opencode', 'kind': 'id', "
                "'value': 'typed-native-sid'}, 'agent_status': 'idle'}}}))\n"
                "else:\n"
                "    print(json.dumps({'error': {'code': 'unexpected-command'}}))\n"
                "    raise SystemExit(1)\n",
                encoding="utf-8")
            fake_herdr.chmod(0o700)
            driver = live.HerdrIntegrationLive(live.build_parser().parse_args([
                "--case", "callbacks", "--output-dir", str(root / "out"),
                "--project", str(project), "--timeout", "10", "--herdr", str(fake_herdr),
            ]))
            self.addCleanup(driver.runtime_dir.cleanup)

            native = driver._agent_start("opencode", "private-pane", name="fixture-agent")

            self.assertTrue(native["start_ok"])
            self.assertTrue(native["session_confirmed_by_get"])
            self.assertEqual(native["session_id"], "typed-native-sid")
            actual = json.loads(captured_calls.read_text(encoding="utf-8"))
            actual_argv = actual["argv"]
            start_at = actual_argv.index("start")
            self.assertEqual(actual_argv[start_at - 1], "agent")
            delimiter = actual_argv.index("--", start_at)
            self.assertEqual(actual_argv[delimiter + 1:], ["--prompt", live.NATIVE_START_PROMPT])
            self.assertEqual(actual_argv.count(live.NATIVE_START_PROMPT), 1)
            self.assertEqual(actual["cwd"], str(project))
            self.assertNotIn(str(project), actual_argv)

            rows = [json.loads(line) for line in driver.commands_path.read_text().splitlines()]
            start_row = next(row for row in rows if row["step"] == "native-opencode-session-start")
            get_row = next(row for row in rows if row["step"] == "native-opencode-session-get")
            self.assertLess(rows.index(start_row), rows.index(get_row))
            self.assertEqual(start_row["argv"][-2:], ["--prompt", "[redacted]"])
            self.assertEqual(start_row["argv"].count("[redacted]"), 1)
            durable = driver.commands_path.read_text()
            self.assertNotIn(live.NATIVE_START_PROMPT, durable)
            self.assertNotIn(str(project), durable)

    def test_claude_start_keeps_existing_no_prompt_behavior(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            driver = live.HerdrIntegrationLive(live.build_parser().parse_args([
                "--case", "callbacks", "--output-dir", str(root / "out"),
                "--project", str(project), "--timeout", "10",
            ]))
            self.addCleanup(driver.runtime_dir.cleanup)
            start = json.dumps({"result": {"agent": {"name": "fixture-agent"}}})
            get = json.dumps({"result": {"agent": {
                "name": "fixture-agent", "agent": "claude", "pane_id": "private-pane",
                "agent_session": {"agent": "claude", "kind": "id", "value": "typed-sid"}}}})
            with mock.patch.object(driver, "herdr_command",
                    side_effect=[(0, start, ""), (0, get, "")]) as command:
                native = driver._agent_start("claude", "private-pane", name="fixture-agent")
            self.assertTrue(native["session_confirmed_by_get"])
            self.assertEqual(native["session_id"], "typed-sid")
            start_args = command.call_args_list[0].args[1]
            self.assertEqual(start_args[:6], [
                "agent", "start", "fixture-agent", "--kind", "claude", "--pane",
            ])
            self.assertNotIn("--", start_args)
            self.assertNotIn("--prompt", start_args)
            self.assertNotIn(live.NATIVE_START_PROMPT, start_args)

    def test_native_session_id_requires_successful_start_and_typed_get(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            driver = live.HerdrIntegrationLive(live.build_parser().parse_args([
                "--case", "callbacks", "--output-dir", str(root / "out"),
                "--project", str(project), "--timeout", "10",
            ]))
            self.addCleanup(driver.runtime_dir.cleanup)
            start_ok = json.dumps({"result": {"agent": {"name": "fixture-agent"}}})
            valid_agent = _native_agent()
            wrong_name = _native_agent(name="other-agent")
            null_name = _native_agent()
            null_name["name"] = None
            wrong_pane = _native_agent(pane="other-pane")
            null_pane = _native_agent()
            null_pane["pane_id"] = None
            wrong_runtime = _native_agent(runtime="opencode", session_runtime="opencode")
            null_runtime = _native_agent()
            null_runtime["agent"] = None
            wrong_session_runtime = _native_agent(session_runtime="opencode")
            missing_session_runtime = _native_agent()
            del missing_session_runtime["agent_session"]["agent"]
            null_session_runtime = _native_agent()
            null_session_runtime["agent_session"]["agent"] = None
            wrong_session_kind = _native_agent(session_kind="session")
            missing_session_kind = _native_agent()
            del missing_session_kind["agent_session"]["kind"]
            null_session_kind = _native_agent()
            null_session_kind["agent_session"]["kind"] = None
            metadata_only = {"name": "fixture-agent", "agent": "codex",
                             "pane_id": "private-pane", "session_id": "metadata-sid"}
            missing_name = _native_agent()
            del missing_name["name"]
            missing_runtime = _native_agent()
            del missing_runtime["agent"]
            missing_pane = _native_agent()
            del missing_pane["pane_id"]
            missing_session = _native_agent()
            del missing_session["agent_session"]
            null_session = _native_agent()
            null_session["agent_session"] = None
            malformed_session = _native_agent()
            malformed_session["agent_session"] = "not-an-object"
            missing_session_id = _native_agent()
            del missing_session_id["agent_session"]["value"]
            null_session_id = _native_agent()
            null_session_id["agent_session"]["value"] = None
            unknown_fields = _native_agent()
            unknown_fields.update({"kind": "opencode", "display_agent": "untrusted-label",
                                   "title": "display only", "metric": {"session_id": "fake"}})
            missing_source = _native_agent()
            del missing_source["agent_session"]["source"]
            identity_scenarios = (
                ("valid-documented-tuple", valid_agent, None, "typed-native-sid", True),
                ("unknown-display-fields-do-not-authorize-or-reject", unknown_fields,
                 None, "typed-native-sid", True),
                ("session-source-is-not-identity-authority", missing_source,
                 None, "typed-native-sid", True),
                ("wrong-requested-name", wrong_name, "native-agent-name-mismatch", None, False),
                ("null-requested-name", null_name, "native-agent-name-mismatch", None, False),
                ("wrong-pane", wrong_pane, "native-agent-pane-mismatch", None, False),
                ("null-pane", null_pane, "native-agent-pane-mismatch", None, False),
                ("wrong-runtime", wrong_runtime, "native-agent-runtime-mismatch", None, False),
                ("null-runtime", null_runtime, "native-agent-runtime-mismatch", None, False),
                ("wrong-session-runtime", wrong_session_runtime,
                 "native-agent-session-runtime-mismatch", None, False),
                ("missing-session-runtime", missing_session_runtime,
                 "native-agent-session-runtime-missing", None, False),
                ("null-session-runtime", null_session_runtime,
                 "native-agent-session-runtime-mismatch", None, False),
                ("invalid-session-kind", wrong_session_kind,
                 "native-agent-session-kind-mismatch", None, False),
                ("missing-session-kind", missing_session_kind,
                 "native-agent-session-kind-missing", None, False),
                ("null-session-kind", null_session_kind,
                 "native-agent-session-kind-mismatch", None, False),
                ("missing-name", missing_name, "native-agent-name-missing", None, False),
                ("missing-runtime", missing_runtime, "native-agent-runtime-missing", None, False),
                ("missing-pane", missing_pane, "native-agent-pane-missing", None, False),
                ("missing-outer-agent", live._NATIVE_IDENTITY_MISSING,
                 "native-agent-tuple-missing", None, False),
                ("null-outer-agent", None,
                 "native-agent-tuple-malformed", None, False),
                ("malformed-outer-agent", ["not", "an", "object"],
                 "native-agent-tuple-malformed", None, False),
                ("missing-session", missing_session,
                 "native-agent-session-missing", None, False),
                ("null-session", null_session,
                 "native-agent-session-malformed", None, False),
                ("malformed-session", malformed_session,
                 "native-agent-session-malformed", None, False),
                ("missing-session-id", missing_session_id,
                 "native-agent-session-id-missing", None, False),
                ("null-session-id", null_session_id,
                 "native-agent-session-id-unusable", None, False),
                ("metadata-id-substitute", metadata_only,
                 "native-agent-session-missing", None, False),
                ("placeholder-dash", _native_agent(sid="-"),
                 "native-agent-session-id-unusable", None, False),
                ("unknown-id", _native_agent(sid="unknown"),
                 "native-agent-session-id-unusable", None, False),
                ("template-id", _native_agent(sid="<sid>"),
                 "native-agent-session-id-unusable", None, False),
            )
            for label, agent, reason, expected_sid, confirmed in identity_scenarios:
                result = ({} if agent is live._NATIVE_IDENTITY_MISSING
                          else {"agent": agent})
                with self.subTest(scenario=label), mock.patch.object(
                        driver, "herdr_command", side_effect=[
                            (0, start_ok, ""),
                            (0, json.dumps({"result": result}), "")]):
                    native = driver._agent_start("codex", "private-pane", name="fixture-agent")
                    self.assertTrue(native["start_ok"])
                    self.assertTrue(native["get_ok"])
                    self.assertEqual(native["session_confirmed_by_get"], confirmed)
                    self.assertEqual(native["session_id"], expected_sid)
                    self.assertEqual(native["session_identity_reason"], reason)

            start_presence_scenarios = (
                ("omitted-outer-agent", {"result": {}}, None, True),
                ("omitted-start-fields", {"result": {"agent": {}}}, None, True),
                ("omitted-start-name", {"result": {"agent": {
                    "agent": "codex", "pane_id": "private-pane"}}}, None, True),
                ("omitted-start-runtime", {"result": {"agent": {
                    "name": "fixture-agent", "pane_id": "private-pane"}}}, None, True),
                ("omitted-start-pane", {"result": {"agent": {
                    "name": "fixture-agent", "agent": "codex"}}}, None, True),
                ("null-start-agent", {"result": {"agent": None}},
                 "native-agent-tuple-malformed", False),
                ("malformed-start-agent", {"result": {"agent": []}},
                 "native-agent-tuple-malformed", False),
                ("null-start-name", {"result": {"agent": {"name": None}}},
                 "native-agent-name-mismatch", False),
                ("null-start-runtime", {"result": {"agent": {"agent": None}}},
                 "native-agent-runtime-mismatch", False),
                ("null-start-pane", {"result": {"agent": {"pane_id": None}}},
                 "native-agent-pane-mismatch", False),
                ("omitted-start-session", {"result": {"agent": {
                    "name": "fixture-agent", "agent": "codex",
                    "pane_id": "private-pane"}}},
                 None, True),
                ("null-start-session", {"result": {"agent": {"agent_session": None}}},
                 "native-agent-session-malformed", False),
                ("malformed-start-session", {"result": {"agent": {"agent_session": []}}},
                 "native-agent-session-malformed", False),
                ("start-session-runtime-omitted", {"result": {"agent": {
                    "agent_session": {"kind": "id", "value": "typed-native-sid"}}}},
                 "native-agent-session-runtime-missing", False),
                ("start-session-runtime-null", {"result": {"agent": {
                    "agent_session": {"agent": None, "kind": "id", "value": "typed-native-sid"}}}},
                 "native-agent-session-runtime-mismatch", False),
                ("start-session-kind-omitted", {"result": {"agent": {
                    "agent_session": {"agent": "codex", "value": "typed-native-sid"}}}},
                 "native-agent-session-kind-missing", False),
                ("start-session-kind-null", {"result": {"agent": {
                    "agent_session": {"agent": "codex", "kind": None,
                                       "value": "typed-native-sid"}}}},
                 "native-agent-session-kind-mismatch", False),
                ("start-session-value-omitted", {"result": {"agent": {
                    "agent_session": {"agent": "codex", "kind": "id"}}}},
                 "native-agent-session-id-missing", False),
                ("start-session-value-null", {"result": {"agent": {
                    "agent_session": {"agent": "codex", "kind": "id", "value": None}}}},
                 "native-agent-session-id-unusable", False),
                ("start-session-valid-subset", {"result": {"agent": {
                    "agent_session": {"agent": "codex", "kind": "id",
                                       "value": "typed-native-sid"}}}}, None, True),
            )
            for label, start_payload, reason, confirmed in start_presence_scenarios:
                with self.subTest(scenario=f"start-{label}"), mock.patch.object(
                        driver, "herdr_command", side_effect=[
                            (0, json.dumps(start_payload), ""),
                            (0, json.dumps({"result": {"agent": valid_agent}}), "")]):
                    native = driver._agent_start("codex", "private-pane", name="fixture-agent")
                    self.assertTrue(native["start_ok"])
                    self.assertTrue(native["get_ok"])
                    self.assertEqual(native["session_confirmed_by_get"], confirmed)
                    self.assertEqual(native["session_id"],
                                     "typed-native-sid" if confirmed else None)
                    self.assertEqual(native["session_identity_reason"], reason)

            sparse_start = json.dumps({"result": {}})
            with self.subTest(scenario="mismatched-get-never-confirms-sparse-start"), \
                    mock.patch.object(driver, "herdr_command", side_effect=[
                        (0, sparse_start, ""),
                        (0, json.dumps({"result": {"agent": wrong_pane}}), "")]):
                native = driver._agent_start("codex", "private-pane", name="fixture-agent")
                self.assertFalse(native["session_confirmed_by_get"])
                self.assertIsNone(native["session_id"])
                self.assertEqual(native["session_identity_reason"],
                                 "native-agent-pane-mismatch")

            start_mismatch = json.dumps({"result": {"agent": {"name": "other-agent"}}})
            with self.subTest(scenario="start-name-mismatch"), mock.patch.object(
                    driver, "herdr_command", side_effect=[
                        (0, start_mismatch, ""),
                        (0, json.dumps({"result": {"agent": valid_agent}}), "")]):
                native = driver._agent_start("codex", "private-pane", name="fixture-agent")
                self.assertTrue(native["start_ok"])
                self.assertTrue(native["get_ok"])
                self.assertFalse(native["session_confirmed_by_get"])
                self.assertIsNone(native["session_id"])
                self.assertEqual(native["session_identity_reason"], "native-agent-name-mismatch")

            for label, start_response, get_response, start_ok_expected, get_ok_expected, reason in (
                    ("get-command-failed", (0, start_ok, ""),
                     (1, json.dumps({"result": {"agent": valid_agent}}), "not accepted"),
                     True, False, "native-agent-get-unavailable"),
                    ("start-command-failed", (1, start_ok, "failed"),
                     (0, json.dumps({"result": {"agent": valid_agent}}), ""),
                     False, True, "native-agent-start-unavailable"),
                    ("start-error-envelope", (0, '{"error":{"code":"start-rejected"}}', ""),
                     (0, json.dumps({"result": {"agent": valid_agent}}), ""),
                     False, True, "native-agent-start-unavailable"),
                    ("get-error-envelope", (0, start_ok, ""),
                     (0, '{"error":{"code":"get-rejected"}}', ""),
                     True, False, "native-agent-get-unavailable")):
                with self.subTest(scenario=label), mock.patch.object(
                        driver, "herdr_command", side_effect=[start_response, get_response]):
                    native = driver._agent_start("codex", "private-pane", name="fixture-agent")
                    self.assertEqual(native["start_ok"], start_ok_expected)
                    self.assertEqual(native["get_ok"], get_ok_expected)
                    self.assertFalse(native["session_confirmed_by_get"])
                    self.assertIsNone(native["session_id"])
                    self.assertEqual(native["session_identity_reason"], reason)

    def test_native_session_id_accepts_opaque_non_placeholder_values(self):
        payload = {"result": {"agent": {"agent_session": {"value": "session:opaque/3"}}}}
        self.assertEqual(live._session_id(payload), "session:opaque/3")

    def test_callback_measurement_keeps_start_identity_and_callback_evidence_distinct(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            driver = live.HerdrIntegrationLive(live.build_parser().parse_args([
                "--case", "callbacks", "--output-dir", str(root / "out"),
                "--project", str(project), "--timeout", "10",
            ]))
            self.addCleanup(driver.runtime_dir.cleanup)
            driver.transport = mock.Mock()
            driver.transport.observe.return_value.complete = True
            native = {"name": "fixture-agent", "session_id": "typed-native-sid",
                      "start_exit": 0, "start_ok": True,
                      "session_confirmed_by_get": True}

            def start_fixture(runtime, _pane):
                (Path(driver.env["HOME"]) / f"callback-{runtime}.log").write_text(
                    f"{runtime}\\n", encoding="utf-8")
                return native

            event = {"session_id": "typed-native-sid", "session_start_source": "startup",
                     "source": "herdr:codex"}
            with mock.patch.object(live, "RUNTIMES", ("codex",)), \
                    mock.patch.object(driver, "_install_and_reinstall",
                                      return_value={"codex": {"status_current": True}}), \
                    mock.patch.object(driver, "create_pane", return_value="private-pane"), \
                    mock.patch.object(driver, "_start_event_tap", return_value=True), \
                    mock.patch.object(driver, "_agent_start", side_effect=start_fixture), \
                    mock.patch.object(driver, "_matching_native_events", return_value=[event]), \
                    mock.patch.object(driver, "_stop_event_tap"):
                result = driver._install_callbacks()

            callback = result["runtimes"]["codex"]
            self.assertEqual(callback["outcome"], "measured")
            self.assertTrue(callback["native_start_ok"])
            self.assertTrue(callback["native_session_confirmed_by_get"])
            self.assertEqual(callback["native_callback_report_count"], 1)
            self.assertEqual(callback["user_sentinel_count"], 1)

    def test_unconfirmed_native_identity_cannot_attribute_a_sessionless_callback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            driver = live.HerdrIntegrationLive(live.build_parser().parse_args([
                "--case", "callbacks", "--output-dir", str(root / "out"),
                "--project", str(project), "--timeout", "10",
            ]))
            self.addCleanup(driver.runtime_dir.cleanup)
            native = {"name": "fixture-agent", "session_id": None,
                      "start_exit": 0, "start_ok": True, "get_exit": 0, "get_ok": True,
                      "session_confirmed_by_get": False,
                      "session_identity_reason": "native-agent-session-id-unusable"}

            def start_fixture(runtime, _pane):
                (Path(driver.env["HOME"]) / f"callback-{runtime}.log").write_text(
                    "codex\n", encoding="utf-8")
                return native

            unbound_event = {"session_id": None, "session_start_source": "startup",
                             "source": "herdr:codex"}
            with mock.patch.object(live, "RUNTIMES", ("codex",)), \
                    mock.patch.object(driver, "_install_and_reinstall",
                                      return_value={"codex": {"status_current": True}}), \
                    mock.patch.object(driver, "create_pane", return_value="private-pane"), \
                    mock.patch.object(driver, "_start_event_tap", return_value=True), \
                    mock.patch.object(driver, "_agent_start", side_effect=start_fixture), \
                    mock.patch.object(driver, "_matching_native_events",
                                      return_value=[unbound_event]) as matching_events, \
                    mock.patch.object(driver, "_stop_event_tap"):
                result = driver._install_callbacks()

            callback = result["runtimes"]["codex"]
            self.assertEqual(callback["outcome"], "unavailable")
            self.assertFalse(callback["native_session_confirmed_by_get"])
            self.assertEqual(callback["native_session_identity_reason"],
                             "native-agent-session-id-unusable")
            self.assertEqual(callback["native_callback_report_count"], 0)
            self.assertEqual(callback["native_callback_reports"], [])
            matching_events.assert_not_called()

    def test_isolation_drops_inherited_credentials_and_keeps_output_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            output = root / "output"
            with mock.patch.dict(os.environ, {
                "OPENAI_API_KEY": "must-not-copy",
                "HERDR_SOCKET_PATH": "/run/default-herdr.sock",
                "CODEX_THREAD_ID": "foreign-thread",
            }, clear=True):
                driver = live.HerdrIntegrationLive(live.build_parser().parse_args([
                    "--case", "callbacks", "--output-dir", str(output),
                    "--project", str(project), "--timeout", "10",
                ]))
            self.addCleanup(driver.runtime_dir.cleanup)
            self.assertNotIn("OPENAI_API_KEY", driver.env)
            self.assertNotIn("HERDR_SOCKET_PATH", driver.env)
            self.assertNotIn("CODEX_THREAD_ID", driver.env)
            self.assertEqual(driver.result["isolation"]["credentials_copied"], False)
            self.assertEqual(Path(driver.env["HOME"]).parent, driver.runtime_root / "isolated")

    def test_command_evidence_keeps_exit_and_hash_but_not_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            driver = live.HerdrIntegrationLive(live.build_parser().parse_args([
                "--case", "codex-busy", "--output-dir", str(root / "out"),
                "--project", str(project), "--timeout", "10",
            ]))
            self.addCleanup(driver.runtime_dir.cleanup)
            rc, stdout, _stderr = driver.command(
                "fixture", ["sh", "-c", "printf '%s' \"$PAYLOAD\""],
                env={**driver.env, "PAYLOAD": "private-output"})
            self.assertEqual(rc, 0)
            self.assertEqual(stdout, "private-output")
            recorded = driver.commands_path.read_text(encoding="utf-8")
            self.assertNotIn("private-output", recorded)
            row = json.loads(recorded)
            self.assertEqual(row["step"], "fixture")
            self.assertEqual(row["returncode"], 0)
            self.assertTrue(row["stdout_hmac"])
            self.assertTrue(row["stderr_reason"] == "exit-0")

    def test_opencode_sentinel_is_seeded_at_the_upstream_home_consumer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            driver = live.HerdrIntegrationLive(live.build_parser().parse_args([
                "--case", "callbacks", "--output-dir", str(root / "out"),
                "--project", str(project), "--timeout", "10",
            ]))
            self.addCleanup(driver.runtime_dir.cleanup)
            driver._seed_user_callback("opencode")
            official = Path(driver.env["HOME"]) / ".config" / "opencode"
            payload = json.loads((official / "opencode.json").read_text())
            self.assertEqual(driver.env["OPENCODE_CONFIG_DIR"], str(official))
            self.assertTrue((official / payload["plugin"][0]).is_file())
            self.assertNotEqual(official, Path(driver.env["XDG_CONFIG_HOME"]) / "opencode")

    def test_busy_probe_uses_command_wrapper_and_records_redacted_native_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            fake_herdr = root / "herdr-fixture"
            fake_herdr.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "args = sys.argv[1:]\n"
                "if 'prompt' in args:\n"
                "    state = 'working'\n"
                "else:\n"
                "    state = args[args.index('--until') + 1]\n"
                "print(json.dumps({'result': {'agent': {'agent_status': state}}}))\n",
                encoding="utf-8")
            fake_herdr.chmod(0o700)
            driver = live.HerdrIntegrationLive(live.build_parser().parse_args([
                "--case", "codex-busy", "--output-dir", str(root / "out"),
                "--project", str(project), "--timeout", "10", "--herdr", str(fake_herdr),
            ]))
            self.addCleanup(driver.runtime_dir.cleanup)
            driver.herdr = str(fake_herdr)

            outcome = driver._busy_probe("codex", "private-pane", {
                "name": "private-agent", "pane_id": "private-pane", "session_id": "native-sid",
                "get_reason": "exit-0", "session_confirmed_by_get": True,
            })

            self.assertTrue(outcome["working_wait"]["matched_requested_state"])
            self.assertTrue(outcome["idle_wait"]["matched_requested_state"])
            self.assertTrue(outcome["native_metadata"]["working_observed"])
            self.assertFalse(outcome["native_metadata"]["stream_metadata_observed"])
            self.assertFalse(outcome["advisory_enqueue"]["accepted"])
            self.assertEqual(outcome["outcome"], "unavailable")
            rows = [json.loads(line) for line in driver.commands_path.read_text().splitlines()]
            prompt = next(row for row in rows if row["step"] == "native-codex-busy-probe")
            self.assertTrue(prompt["native_output_not_recorded"])
            self.assertIn("[redacted]", prompt["argv"])
            self.assertTrue(prompt["stdout_hmac"])
            rendered = driver.commands_path.read_text(encoding="utf-8")
            self.assertNotIn("Run one bounded shell command", rendered)
            self.assertNotIn('"agent_status": "working"', rendered)

    def test_zero_exit_error_envelope_does_not_count_as_native_working(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            fake_herdr = root / "herdr-fixture"
            fake_herdr.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "if 'prompt' in sys.argv:\n"
                "    print(json.dumps({'error': {'code': 'native_auth_required'}}))\n"
                "else:\n"
                "    print(json.dumps({'result': {'agent': {'agent_status': 'working'}}}))\n",
                encoding="utf-8")
            fake_herdr.chmod(0o700)
            driver = live.HerdrIntegrationLive(live.build_parser().parse_args([
                "--case", "codex-busy", "--output-dir", str(root / "out"),
                "--project", str(project), "--timeout", "10", "--herdr", str(fake_herdr),
            ]))
            self.addCleanup(driver.runtime_dir.cleanup)
            driver.herdr = str(fake_herdr)

            outcome = driver._busy_probe("codex", "private-pane", {
                "name": "private-agent", "pane_id": "private-pane", "session_id": "native-sid",
                "get_reason": "exit-0", "session_confirmed_by_get": True,
            })

            self.assertFalse(outcome["prompt"]["accepted"])
            self.assertEqual(outcome["prompt"]["error_code"], "native_auth_required")
            self.assertFalse(outcome["native_metadata"]["working_observed"])
            self.assertIsNone(outcome["working_wait"])
            self.assertIsNone(outcome["idle_wait"])
            self.assertEqual(outcome["outcome"], "unavailable")

    def test_seeded_native_callback_redirect_keeps_a_spaced_private_path_as_one_target(self):
        with tempfile.TemporaryDirectory(prefix="hearting callback ") as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            driver = live.HerdrIntegrationLive(live.build_parser().parse_args([
                "--case", "callbacks", "--output-dir", str(root / "out"),
                "--project", str(project), "--timeout", "10",
            ]))
            self.addCleanup(driver.runtime_dir.cleanup)
            for runtime, event in (("claude", "SessionStart"), ("codex", "SessionStart")):
                with self.subTest(runtime=runtime):
                    log = driver._seed_user_callback(runtime)
                    config = Path(driver.env["CLAUDE_CONFIG_DIR"] if runtime == "claude"
                                  else driver.env["CODEX_HOME"]) / (
                                      "settings.json" if runtime == "claude" else "hooks.json")
                    payload = json.loads(config.read_text(encoding="utf-8"))
                    if runtime == "claude":
                        command = payload["hooks"][event][0]["hooks"][0]["command"]
                    else:
                        command = payload["hooks"][event][0]["hooks"][0]["command"]
                    argv = shlex.split(command)
                    self.assertEqual(argv[:3], ["printf", runtime, ">>"])
                    self.assertEqual(argv[3], str(log))

    def test_long_evidence_path_can_bind_a_private_socket_and_failure_keeps_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            output = root / ("long-evidence-" + "a" * 100) / ("b" * 100)
            driver = live.HerdrIntegrationLive(live.build_parser().parse_args([
                "--case", "events-lost", "--output-dir", str(output),
                "--project", str(project), "--timeout", "10",
            ]))
            self.addCleanup(driver.runtime_dir.cleanup)
            runtime = driver.runtime_root
            endpoint = Path(driver.env["HERDR_CONFIG_PATH"]).parent / "sessions" / driver.session / "herdr-client.sock"
            endpoint.parent.mkdir(parents=True)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(endpoint))
            with mock.patch.object(driver, "_case", return_value={"outcome": "unavailable", "reason": "fixture"}), \
                    mock.patch("sys.stdout"):
                self.assertEqual(driver.execute(), 2)
            self.assertFalse(runtime.exists())
            self.assertTrue(driver.result_path.is_file())
            result = json.loads(driver.result_path.read_text())
            self.assertIn({"action": "remove-private-runtime", "result": "removed"}, result["cleanup"])

    def test_startup_error_keeps_classification_without_raw_server_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            driver = live.HerdrIntegrationLive(live.build_parser().parse_args([
                "--case", "events-lost", "--output-dir", str(root / "out"),
                "--project", str(project), "--timeout", "10",
            ]))
            self.addCleanup(driver.runtime_dir.cleanup)
            driver.herdr = "/fixture/herdr"
            server = mock.Mock(pid=2147483647)
            server.poll.return_value = 1

            def start(_argv, **kwargs):
                kwargs["stderr"].write(b"private startup text: socket name length exceeds capacity of sun_path")
                kwargs["stderr"].flush()
                return server

            with mock.patch.object(live.subprocess, "Popen", side_effect=start):
                self.assertFalse(driver.start_server())
            evidence = driver.commands_path.read_text()
            self.assertNotIn("private startup text", evidence)
            last = json.loads(evidence.splitlines()[-1])
            self.assertEqual(last["stderr_reason"], "local-socket-path-too-long")
            self.assertEqual(last["returncode"], 1)
            self.assertTrue(last["stderr_hmac"])

    def test_event_tap_uses_private_endpoint_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            driver = live.HerdrIntegrationLive(live.build_parser().parse_args([
                "--case", "events-lost", "--output-dir", str(root / "out"),
                "--project", str(project), "--timeout", "10",
            ]))
            self.addCleanup(driver.runtime_dir.cleanup)
            driver.env["HERDR_SOCKET_PATH"] = "/private/owned/herdr.sock"
            sys.path.insert(0, str(ROOT / "utilities"))
            self.addCleanup(sys.path.remove, str(ROOT / "utilities"))
            import herdr_observation
            with mock.patch.dict(os.environ, {
                    "HERDR_SOCKET_PATH": "/foreign/herdr.sock",
                    "AGENT_HERDR_SESSION": "foreign-session"}), \
                    mock.patch.object(herdr_observation, "ObservationTransport") as transport:
                transport.return_value.observe.return_value.complete = True
                try:
                    self.assertTrue(driver._start_event_tap())
                    transport.assert_called_once_with(
                        driver.session, "/private/owned/herdr.sock", timeout=0.5)
                finally:
                    driver._stop_event_tap()


if __name__ == "__main__":
    unittest.main()
