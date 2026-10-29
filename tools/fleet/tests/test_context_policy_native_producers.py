#!/usr/bin/env python3
"""Claude and Codex applied-policy producer -> collector -> normalizer -> JSON/render.

Each case starts from the real producer surface (the statusline script's tap, the
registered wrapper's launch metadata, Codex's rollout/stream facts and model catalogue
cache) and ends at the Fleet JSON payload and the rendered context row. The native
formulas under test are the verified consumers recorded in ``context_policy.py``.
"""
import importlib.util
from datetime import datetime, timezone
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_TOOLS_DIR = str(Path(__file__).resolve().parents[2])
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import projection, render                      # noqa: E402
from fleet.collectors import claude, codex                # noqa: E402
from fleet.collectors import dispatch as dispatch_collector  # noqa: E402
from fleet.model import DispatchJob, Session              # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
if not (ROOT / "adapters" / "claude").is_dir():           # the byte-identical mirror layout
    ROOT = Path(__file__).resolve().parents[5]
STATUSLINE = ROOT / "adapters" / "claude" / "statusline.sh"
WRAPPER = ROOT / "adapters" / "claude" / "bin" / "dispatch-headless.py"

CLAUDE_VERSION = "2.1.299"
CODEX_VERSION = "0.162.1"
CODEX_SID = "019facc9-fe83-7480-89f6-9e64fbebf0cb"


def _plain(rows):
    return "".join(part for row in rows for part, _key in row)


def _statusline_tap(home, *, sid, env=None, window=1_000_000, active=400_000,
                    version=CLAUDE_VERSION, model="claude-opus-5-5", pct=40):
    """Run the real statusline script; return the tap JSON it wrote for ``sid``."""
    payload = {"session_id": sid, "cwd": str(home), "version": version,
               "model": {"id": model, "display_name": "Opus"},
               "context_window": {
                   "context_window_size": window, "used_percentage": pct,
                   "current_usage": {"input_tokens": active, "cache_creation_input_tokens": 0,
                                     "cache_read_input_tokens": 0}}}
    child_env = {key: value for key, value in os.environ.items()
                 if not key.startswith(("CLAUDE_", "DISABLE_"))}
    child_env.update({"CLAUDE_CONFIG_DIR": str(home), "HOME": str(home), **(env or {})})
    subprocess.run(["bash", str(STATUSLINE)], input=json.dumps(payload).encode(),
                   env=child_env, check=True, capture_output=True, timeout=30)
    return json.loads((Path(home) / ".statusline" / (sid + ".json")).read_text())


class ClaudeInteractiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def _session(self, tap, sid="sid-claude"):
        session = Session(harness="claude", pid=4242, session_id=sid, cwd=str(self.home),
                          liveness="working")
        claude._apply_statusline(session, tap)
        return session

    def _json_and_row(self, session):
        projection.attach_projections([session], [], now=time.time())
        return session.to_dict(), _plain(render._context_detail_row(session, term_width=140))

    def test_env_window_gives_applied_trigger_and_matching_percent_and_track(self):
        tap = _statusline_tap(self.home, sid="sid-env",
                              env={"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "800000"})
        session = self._session(tap, "sid-env")
        payload, row = self._json_and_row(session)
        # raw 1M capacity and Claude's own 40% stay diagnostics; the trigger is
        # 800000 - 20000 output reserve - 13000 compaction buffer.
        self.assertEqual(payload["context_window_tokens"], 1_000_000)
        self.assertEqual(payload["context_display_window_tokens"], 767_000)
        self.assertEqual(payload["context_policy"]["raw_percentage"], 40)
        self.assertEqual(payload["context_policy"]["state"], "applied")
        self.assertEqual(payload["context_policy"]["runtime_version"], CLAUDE_VERSION)
        self.assertEqual(payload["ctx_pct"], 52)
        self.assertEqual(payload["context"]["used_pct"], 52)
        self.assertIn("52%", row)

    def test_pct_override_lowers_the_trigger_once(self):
        tap = _statusline_tap(self.home, sid="sid-pct", active=100_000,
                              env={"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "800000",
                                   "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"})
        session = self._session(tap, "sid-pct")
        self.assertEqual(session.context_display_window_tokens, 390_000)
        self.assertEqual(session.ctx_pct, 26)

    def test_known_zero_is_zero_percent(self):
        tap = _statusline_tap(self.home, sid="sid-zero", active=0, pct=0,
                              env={"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "800000"})
        session = self._session(tap, "sid-zero")
        self.assertEqual(session.active_context_tokens, 0)
        self.assertEqual(session.ctx_pct, 0)

    def test_missing_disabled_and_unverified_inputs_stay_unknown(self):
        cases = {
            "no-window": (dict(), {}, "native-window-source-not-env"),
            "disabled": ({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "800000",
                          "DISABLE_AUTO_COMPACT": "1"}, {},
                         "native-auto-compaction-disabled"),
            "version": ({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "800000"}, {"version": "9.9.9"},
                        "native-consumer-version-unverified"),
        }
        for name, (env, extra, reason) in cases.items():
            with self.subTest(name):
                tap = _statusline_tap(self.home, sid="sid-" + name, env=env, **extra)
                session = self._session(tap, "sid-" + name)
                self.assertEqual(session.context_policy["reason"], reason)
                self.assertIsNone(session.ctx_pct)
                self.assertIsNone(session.context_display_window_tokens)
                self.assertEqual(session.active_context_tokens, 400_000)  # raw is preserved

    def test_settings_file_disabling_auto_compact_is_unknown(self):
        (self.home / "settings.json").write_text(json.dumps({"autoCompactEnabled": False}))
        tap = _statusline_tap(self.home, sid="sid-settings",
                              env={"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "800000"})
        session = self._session(tap, "sid-settings")
        self.assertEqual(session.context_policy["state"], "disabled")
        self.assertIsNone(session.ctx_pct)

    def test_raw_window_is_not_guessed_when_the_callback_omits_it(self):
        tap = _statusline_tap(self.home, sid="sid-nowin", window=0,
                              env={"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "800000"})
        tap["context_window"].pop("context_window_size")
        session = self._session(tap, "sid-nowin")
        self.assertIsNone(session.context_window_tokens)
        self.assertIsNone(session.ctx_pct)

    def test_statusline_foreign_sid_does_not_supply_context(self):
        tap = _statusline_tap(self.home, sid="sid-other", window=1_000_000)
        session = self._session(tap, "sid-selected")
        self.assertIsNone(session.active_context_tokens)
        self.assertIsNone(session.context_window_tokens)
        self.assertIsNone(session.context_policy)

    def test_repeated_statusline_does_not_keep_a_missing_or_malformed_raw_pair(self):
        tap = _statusline_tap(self.home, sid="sid-same")
        session = self._session(tap, "sid-same")
        for context in ({"total_input_tokens": 999_999}, [], "malformed"):
            with self.subTest(context=context):
                claude._apply_statusline(session, {"session_id": "sid-same", "context_window": context})
                self.assertIsNone(session.active_context_tokens)
                self.assertIsNone(session.context_window_tokens)
                self.assertIsNone(session.ctx_pct)


def _wrapper_module():
    for entry in (ROOT / "utilities", ROOT / "adapters" / "claude" / "bin"):
        if str(entry) not in sys.path:
            sys.path.insert(0, str(entry))
    spec = importlib.util.spec_from_file_location("claude_wrapper_policy_producers", WRAPPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ClaudeRegisteredTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs = os.path.join(self.tmp.name, ".dispatch", "logs")
        os.makedirs(self.logs)
        patcher = mock.patch.dict(os.environ, {"AGENT_HOME": self.tmp.name,
                                               "CLAUDE_CONFIG_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        dispatch_collector._CLAUDE_STREAM_CACHE.clear()
        self.addCleanup(dispatch_collector._CLAUDE_STREAM_CACHE.clear)

    def _job(self, env, rows, settings_env=None):
        home = os.path.join(self.tmp.name, "worker-home")
        os.makedirs(home, exist_ok=True)
        Path(home, "settings.json").write_text(json.dumps({"env": settings_env or {}}))
        job = DispatchJob(key="code", slug="worker", pid=91, proc_start="w", harness="claude",
                          attempt_id="att-claude-reg", is_child=True, liveness="working")
        job._log_file = os.path.join(self.logs, "worker.att-claude-reg.claude.jsonl")
        job._registry_metadata = _wrapper_module().claude_compaction_inputs(
            {"CLAUDE_CONFIG_DIR": home, **env})
        Path(job._log_file).write_text("".join(json.dumps(row) + "\n" for row in rows))
        return job

    @staticmethod
    def _rows(active, *, result=True, version=CLAUDE_VERSION):
        rows = [
            {"type": "system", "subtype": "init", "session_id": "sid-reg",
             "claude_code_version": version, "model": "claude-opus-5-5"},
            {"type": "assistant", "session_id": "sid-reg", "message": {
                "model": "claude-opus-5-5", "usage": {
                    "input_tokens": active, "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0}}},
        ]
        if result:
            rows.append({"type": "result", "session_id": "sid-reg", "modelUsage": {
                "claude-opus-5-5": {"contextWindow": 1_000_000, "maxOutputTokens": 128_000}}})
        return rows

    def _collect(self, job):
        dispatch_collector._enrich_claude_stream_session(job)
        projection.attach_projections([], [job], now=time.time())
        return job.to_dict(), _plain(render._dispatch_summary_detail_row(
            job, depth=2, term_width=168))

    def test_launch_window_plus_terminal_raw_window_gives_the_applied_trigger(self):
        job = self._job({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "800000"}, self._rows(383_500))
        payload, row = self._collect(job)
        self.assertEqual(payload["context_window_tokens"], 1_000_000)
        self.assertEqual(payload["context_display_window_tokens"], 767_000)
        self.assertEqual(payload["context_policy"]["attempt_id"], "att-claude-reg")
        self.assertEqual(payload["ctx_pct"], 50)
        self.assertIn("50%", row)

    def test_another_models_result_window_is_not_a_window_for_the_latest_model(self):
        rows = self._rows(123_456)
        rows[-1]["modelUsage"] = {"claude-sonnet-5-5": {
            "contextWindow": 1_000_000, "maxOutputTokens": 128_000}}
        job = self._job({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "800000"}, rows)
        payload, _ = self._collect(job)
        self.assertEqual(payload["active_context_tokens"], 123_456)
        self.assertIsNone(payload["context_window_tokens"])
        self.assertIsNone(payload["context_display_window_tokens"])
        self.assertIsNone(payload["ctx_pct"])

    def test_launch_override_and_disable_are_honoured(self):
        job = self._job({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "800000",
                         "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"}, self._rows(195_000))
        payload, _row = self._collect(job)
        self.assertEqual(payload["context_display_window_tokens"], 390_000)
        self.assertEqual(payload["ctx_pct"], 50)
        disabled = self._job({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "800000",
                              "DISABLE_COMPACT": "1"}, self._rows(195_000))
        payload, _row = self._collect(disabled)
        self.assertEqual(payload["context_policy"]["state"], "disabled")
        self.assertIsNone(payload["ctx_pct"])

    def test_live_run_before_the_terminal_window_never_guesses_one_million(self):
        job = self._job({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "800000"},
                        self._rows(300_000, result=False))
        payload, row = self._collect(job)
        self.assertEqual(payload["active_context_tokens"], 300_000)
        self.assertIsNone(payload["context_window_tokens"])
        self.assertIsNone(payload["ctx_pct"])
        self.assertNotIn("%", row.replace("—", ""))

    def test_a_small_launch_window_is_provable_before_the_terminal_row(self):
        job = self._job({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "150000"},
                        self._rows(58_500, result=False))
        payload, _row = self._collect(job)
        self.assertEqual(payload["context_display_window_tokens"], 117_000)
        self.assertEqual(payload["ctx_pct"], 50)

    def test_disagreeing_settings_env_does_not_choose_either_value(self):
        job = self._job({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "800000"}, self._rows(1_000),
                        settings_env={"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "300000"})
        payload, _row = self._collect(job)
        self.assertEqual(payload["context_policy"]["reason"],
                         "launch-env-and-settings-env-disagree")
        self.assertIsNone(payload["ctx_pct"])

    def test_parent_or_old_attempt_metadata_is_not_borrowed(self):
        job = self._job({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "800000"}, self._rows(1_000))
        job._registry_metadata = {}
        payload, _row = self._collect(job)
        self.assertEqual(payload["context_policy"]["reason"],
                         "launch-compaction-inputs-not-recorded")
        self.assertIsNone(payload["ctx_pct"])


class _CodexFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "codex-home")
        self.cwd = os.path.join(self.tmp.name, "project")
        os.makedirs(self.cwd)
        patcher = mock.patch.dict(os.environ, {"CODEX_HOME": self.home, "HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        from fleet.context_policy import _CODEX_EXECUTION_INPUTS
        _CODEX_EXECUTION_INPUTS.clear()
        self.addCleanup(_CODEX_EXECUTION_INPUTS.clear)
        for patcher in (
                mock.patch.object(codex, "_process_started_at",
                                  side_effect=lambda _: getattr(self, "execution_start", None)),
                mock.patch.object(codex, "_cmdline_overrides_compaction", return_value=False)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def catalogue(self, *, version=CODEX_VERSION, percent=95, slug="gpt-6.1-sol",
                  limit=None):
        os.makedirs(self.home, exist_ok=True)
        if not Path(self.home, "config.toml").exists():
            Path(self.home, "config.toml").write_text("")
        Path(self.home, "models_cache.json").write_text(json.dumps({
            "client_version": version,
            "models": [{"slug": slug, "context_window": 500000, "max_context_window": 872000,
                        "effective_context_window_percent": percent,
                        "auto_compact_token_limit": limit}]}))

    def config(self, text):
        os.makedirs(self.home, exist_ok=True)
        Path(self.home, "config.toml").write_text(text)

    def test_ambiguous_native_window_never_invents_a_trigger(self):
        for percent, usable in ((33, 28500), (37, 37001)):
            with self.subTest(percent=percent):
                self.catalogue(percent=percent)
                payload, row = self._policy_case(usable=usable, active=10000)
                self.assertEqual(payload["context_window_tokens"], usable)
                self.assertEqual(payload["active_context_tokens"], 10000)
                self.assertEqual(payload["context_policy"]["state"], "unknown")
                self.assertEqual(payload["context_policy"]["reason"],
                                 "codex-applied-trigger-not-uniquely-determined")
                self.assertIsNone(payload["context_policy"]["effective_window_tokens"])
                self.assertIsNone(payload["context_display_window_tokens"])
                self.assertIsNone(payload["ctx_pct"])
                # No trigger is invented; the row keeps the native gauge of the raw window.
                from fleet.token_budget import codex_context_used_pct
                self.assertIn(f"{codex_context_used_pct(10000, usable)}%", row)

    def test_limit_clamp_can_resolve_the_ambiguous_native_window(self):
        self.catalogue(percent=33)
        self.config("model_auto_compact_token_limit = 20000\n")
        payload, row = self._policy_case(usable=28500, active=10000)
        self.assertEqual(payload["context_window_tokens"], 28500)
        self.assertIsNone(payload["context_policy"]["effective_window_tokens"])
        self.assertEqual(payload["context_policy"]["state"], "applied")
        self.assertEqual(payload["context_display_window_tokens"], 20000)
        self.assertEqual(payload["ctx_pct"], 50)
        self.assertIn("50%", row)

    def rollout(self, *, version=CODEX_VERSION, model="gpt-6.1-sol", usable=760_000,
                active=360_000, sid=CODEX_SID):
        directory = os.path.join(self.home, "sessions", "2026", "10", "14")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "rollout-2026-10-14T22-23-02-%s.jsonl" % sid)
        self.execution_start = time.time()
        created = datetime.fromtimestamp(self.execution_start + 0.001,
                                         timezone.utc).isoformat()
        rows = [
            {"type": "session_meta", "payload": {"id": sid, "cwd": self.cwd,
                                                 "cli_version": version,
                                                 "timestamp": created}},
            {"type": "turn_context", "payload": {"model": model, "effort": "xhigh"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "last_token_usage": {"total_tokens": active, "input_tokens": active},
                "total_token_usage": {"total_tokens": active * 2},
                "model_context_window": usable}}},
        ]
        Path(path).write_text("".join(json.dumps(row) + "\n" for row in rows))
        return path


class CodexInteractiveTest(_CodexFixture, unittest.TestCase):
    def test_0163_existing_native_760k_does_not_become_current_config_1m(self):
        self.catalogue(version="0.163.0")
        self.config("model_context_window = 1000000\n")
        _, payload, row = self._collect(self.rollout(version="0.163.0"))
        self.assertEqual(payload["context_window_tokens"], 760_000)
        self.assertEqual(payload["active_context_tokens"], 360_000)
        self.assertEqual(payload["session_total_tokens"], 720_000)
        self.assertEqual(payload["context_policy"]["state"], "unknown")
        self.assertEqual(payload["context_policy"]["reason"], "native-consumer-version-unverified")
        self.assertIsNone(payload["context_display_window_tokens"])
        self.assertIn("47%", row)

    def _policy_case(self, **kwargs):
        _session, payload, row = self._collect(self.rollout(**kwargs))
        return payload, row

    def _collect(self, path):
        session = Session(harness="codex", pid=4343, cwd=self.cwd, liveness="working",
                          elapsed_min=5, slug="project")
        session._herdr_session_id = CODEX_SID
        codex._ROLLOUT_VERSION_CACHE.clear()      # each fixture rewrites the same rollout path
        codex._TURN_CONTEXT_CACHE.clear()
        tick = codex._CodexTick(default_home=self.home, proc_paths={4343: path},
                                subagents_by_home={})
        codex.enrich(session, tick=tick)
        projection.attach_projections([session], [], now=time.time())
        return session, session.to_dict(), _plain(
            render._context_detail_row(session, term_width=140))

    def test_native_default_ninety_percent_of_the_resolved_window(self):
        self.catalogue()
        session, payload, row = self._collect(self.rollout())
        # token_count's 760000 is 95% of the configured 800000; auto-compaction fires at
        # 90% of that window (ModelInfo::auto_compact_token_limit), not at the usable cap.
        self.assertEqual(payload["context_window_tokens"], 760_000)
        self.assertIsNone(payload["context_policy"]["effective_window_tokens"])
        self.assertEqual(payload["context_display_window_tokens"], 720_000)
        self.assertEqual(payload["ctx_pct"], 50)
        self.assertEqual(payload["context_policy"]["runtime_version"], CODEX_VERSION)
        self.assertIn("50%", row)

    def test_user_limit_is_clamped_to_the_native_ninety_percent(self):
        self.catalogue()
        self.config("model_auto_compact_token_limit = 400000\n")
        _session, payload, _row = self._collect(self.rollout(active=200_000))
        self.assertEqual(payload["context_display_window_tokens"], 400_000)
        self.assertEqual(payload["ctx_pct"], 50)
        self.config("model_auto_compact_token_limit = 790000\n")
        _session, payload, _row = self._collect(self.rollout(active=200_000))
        self.assertEqual(payload["context_display_window_tokens"], 720_000)

    def test_zero_active_is_known_zero_and_zero_usable_window_is_unknown(self):
        self.catalogue()
        _session, payload, _row = self._collect(self.rollout(active=0))
        self.assertEqual(payload["active_context_tokens"], 0)
        self.assertEqual(payload["ctx_pct"], 0)
        _session, payload, _row = self._collect(self.rollout(usable=0, active=10))
        self.assertIsNone(payload["ctx_pct"])

    def test_unproven_inputs_leave_the_gauge_unknown_but_keep_raw_telemetry(self):
        cases = {
            "version": (lambda: (self.catalogue(), self.rollout(version="9.9.9"))[1],
                        "native-consumer-version-unverified"),
            "catalogue": (lambda: self.rollout(model="not-in-catalogue"),
                          "codex-policy-inputs-not-bound-to-execution"),
            "scope": (lambda: (self.config(
                'model_auto_compact_token_limit_scope = "body_after_prefix"\n'),
                self.rollout())[1], "codex-compaction-config-not-interpretable"),
            "budget": (lambda: (self.config("[features.token_budget]\nenabled = true\n"),
                                self.rollout())[1],
                       "codex-compaction-config-not-interpretable"),
        }
        for name, (build, reason) in cases.items():
            with self.subTest(name):
                for stale in ("models_cache.json", "config.toml"):
                    Path(self.home, stale).unlink(missing_ok=True)
                self.catalogue()
                path = build()
                _session, payload, _row = self._collect(path)
                self.assertEqual(payload["context_policy"]["reason"], reason)
                self.assertIsNone(payload["ctx_pct"])
                self.assertEqual(payload["active_context_tokens"], 360_000)
                self.assertEqual(payload["context_window_tokens"], 760_000)

    def test_cli_override_in_the_process_command_line_is_unknown(self):
        self.catalogue()
        with mock.patch.object(codex, "_cmdline_overrides_compaction", return_value=True):
            _session, payload, _row = self._collect(self.rollout())
        self.assertEqual(payload["context_policy"]["reason"],
                         "codex-cli-compaction-override-present")
        self.assertIsNone(payload["ctx_pct"])


class CodexRegisteredTest(_CodexFixture, unittest.TestCase):
    def _policy_case(self, **kwargs):
        self.rollout(**kwargs)
        return self._collect(self._job(kwargs["usable"], kwargs["active"]))

    def setUp(self):
        super().setUp()
        self.logs = os.path.join(self.tmp.name, ".dispatch", "logs")
        os.makedirs(self.logs)
        extra = mock.patch.dict(os.environ, {"AGENT_HOME": self.tmp.name})
        extra.start()
        self.addCleanup(extra.stop)
        dispatch_collector._CODEX_ATTEMPT_CACHE.clear()
        self.addCleanup(dispatch_collector._CODEX_ATTEMPT_CACHE.clear)
        codex._ROLLOUT_VERSION_CACHE.clear()

    def _job(self, usable, active, thread=CODEX_SID):
        job = DispatchJob(key="code", slug="worker", pid=91, proc_start="w", harness="codex",
                          attempt_id="att-codex-reg", is_child=True, liveness="working")
        job.cwd = self.cwd
        job._context_execution_started_at = getattr(self, "execution_start", time.time())
        job._log_file = os.path.join(self.logs, "worker.att-codex-reg.codex.jsonl")
        rows = [
            {"type": "thread.started", "thread_id": thread},
            {"type": "dispatch.supervisor.token_usage", "thread_id": thread, "turn_id": "turn-1",
             "timestamp": "2026-10-14T13:30:00Z",
             "token_usage": {"model_context_window": usable,
                             "last": {"total_tokens": active},
                             "total": {"total_tokens": active * 2}}},
        ]
        Path(job._log_file).write_text("".join(json.dumps(row) + "\n" for row in rows))
        return job

    def _collect(self, job):
        dispatch_collector._enrich_codex_attempt_session(job)
        projection.attach_projections([], [job], now=time.time())
        return job.to_dict(), _plain(render._dispatch_summary_detail_row(
            job, depth=2, term_width=168))

    def test_supervisor_stream_and_exact_rollout_give_the_applied_trigger(self):
        self.catalogue()
        self.rollout()
        payload, row = self._collect(self._job(760_000, 360_000))
        self.assertEqual(payload["context_window_tokens"], 760_000)
        self.assertEqual(payload["context_display_window_tokens"], 720_000)
        self.assertEqual(payload["context_policy"]["attempt_id"], "att-codex-reg")
        self.assertEqual(payload["ctx_pct"], 50)
        self.assertIn("50%", row)

    def test_smaller_native_window_and_zero_active(self):
        self.catalogue()
        self.rollout(usable=190_000)
        payload, _row = self._collect(self._job(190_000, 0))
        self.assertEqual(payload["context_display_window_tokens"], 180_000)
        self.assertEqual(payload["ctx_pct"], 0)

    def test_missing_rollout_or_catalogue_is_unknown_not_a_universal_ratio(self):
        payload, _row = self._collect(self._job(760_000, 360_000))
        self.assertIsNone(payload["ctx_pct"])
        self.assertEqual(payload["active_context_tokens"], 360_000)
        self.assertEqual(payload["context_policy"]["state"], "unknown")

    def test_other_thread_rollout_is_not_borrowed(self):
        self.catalogue()
        self.rollout(sid="019facc9-fe83-7480-89f6-9e64fbebf0cc")
        payload, _row = self._collect(self._job(760_000, 360_000))
        self.assertIsNone(payload["ctx_pct"])

    def test_private_startup_catalogue_reaches_the_dispatch_gauge(self):
        self.catalogue()
        path = self.rollout(active=200_000)
        catalogue = Path(self.home, "models_cache.json")
        document = json.loads(catalogue.read_text())
        document["models"][0]["auto_compact_token_limit"] = 400_000
        catalogue.write_text(json.dumps(document))
        rows = [json.loads(line) for line in Path(path).read_text().splitlines()]
        rows[0]["payload"]["timestamp"] = datetime.fromtimestamp(
            time.time() + 0.001, timezone.utc).isoformat()
        Path(path).write_text("".join(json.dumps(row) + "\n" for row in rows))
        job = self._job(760_000, 200_000)
        job._registry_metadata = {"runtime_home": self.home}
        with mock.patch("fleet.collectors.dispatch._native_home_admitted", return_value=True):
            payload, row = self._collect(job)
        self.assertEqual(payload["context_display_window_tokens"], 400_000)
        self.assertEqual(payload["ctx_pct"], 50)
        self.assertIn("50%", row)

    def test_dispatch_does_not_replace_its_home_with_a_parent_catalogue(self):
        self.catalogue()
        self.rollout()
        other = Path(self.tmp.name, "other-execution")
        other.mkdir()
        (other / "config.toml").write_text("")
        job = self._job(760_000, 360_000)
        job._registry_metadata = {"runtime_home": str(other)}
        payload, _row = self._collect(job)
        self.assertEqual(payload["active_context_tokens"], 360_000)
        self.assertIsNone(payload["context_display_window_tokens"])
        self.assertIsNone(payload["ctx_pct"])

    def test_later_user_settings_do_not_change_an_existing_attempt(self):
        self.catalogue()
        self.rollout()
        job = self._job(760_000, 360_000)
        before, _row = self._collect(job)
        self.config("model_auto_compact_token_limit = 400000\n")
        after, _row = self._collect(job)
        self.assertEqual(before["context_display_window_tokens"], 720_000)
        self.assertEqual(after["context_display_window_tokens"], 720_000)


if __name__ == "__main__":
    unittest.main()
