#!/usr/bin/env python3
"""Exact native context-policy sidecar ownership checks."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

_TOOLS_DIR = str(Path(__file__).resolve().parents[2])
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet.context_policy import SCHEMA, policy_path, read_policy
from fleet.context_policy import codex_applied_policy, _CODEX_EXECUTION_INPUTS


class ContextPolicyReaderTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = mock.patch.dict(os.environ, {
            "XDG_STATE_HOME": str(Path(self.temp.name) / "state"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("FLEET_SESSION_REGISTRY_DIR", None)

    def _write(self, *, attempt_id=None, session_id="ses_abc123", model="p/m"):
        path = policy_path("opencode", pid=123, proc_start="456", attempt_id=attempt_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "schema": SCHEMA, "harness": "opencode", "session_id": session_id,
            "pid": 123, "proc_start": "456", "attempt_id": attempt_id,
            "model": model, "observed_at": 1000.0,
            "policy": {"state": "applied", "source": "native-test",
                       "effective_window_tokens": 100_000,
                       "applied_trigger_tokens": 80_000},
        }
        path.write_text(json.dumps(record), encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def test_interactive_policy_requires_exact_pid_start_session_and_model(self):
        self._write()
        args = dict(harness="opencode", pid=123, proc_start="456", session_id="ses_abc123", model="p/m")
        self.assertEqual(read_policy(**args)["policy"]["applied_trigger_tokens"], 80_000)
        self.assertIsNone(read_policy(**{**args, "session_id": "ses_other"}))
        self.assertIsNone(read_policy(**{**args, "proc_start": "457"}))
        self.assertIsNone(read_policy(**{**args, "model": "p/other"}))

    def test_attempt_policy_requires_exact_attempt_and_session(self):
        self._write(attempt_id="att-1")
        args = dict(harness="opencode", attempt_id="att-1", session_id="ses_abc123")
        self.assertIsNotNone(read_policy(**args))
        self.assertIsNone(read_policy(**{**args, "attempt_id": "att-2"}))
        self.assertIsNone(read_policy(**{**args, "session_id": "ses_other"}))

    def test_symlinked_policy_is_not_read(self):
        path = self._write()
        replacement = path.with_suffix(".link")
        replacement.symlink_to(path)
        path.unlink()
        replacement.rename(path)
        self.assertIsNone(read_policy(harness="opencode", pid=123, proc_start="456",
                                      session_id="ses_abc123", model="p/m"))

    def test_conflicting_nested_session_or_attempt_is_not_rebound(self):
        for field, foreign in (("session_id", "ses_other"), ("attempt_id", "att-other")):
            with self.subTest(field=field):
                path = self._write(attempt_id="att-1")
                record = json.loads(path.read_text())
                record["policy"][field] = foreign
                path.write_text(json.dumps(record))
                self.assertIsNone(read_policy(harness="opencode", attempt_id="att-1",
                                              session_id="ses_abc123", model="p/m"))


class CodexStartupPolicyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / "models_cache.json").write_text(json.dumps({
            "client_version": "0.162.1", "models": [{
                "slug": "test-model", "effective_context_window_percent": 95,
                "auto_compact_token_limit": None}]}))
        (self.home / "config.toml").write_text("model_auto_compact_token_limit = 100000\n")
        # Leave a deterministic gap between fixture creation and the captured
        # execution clock. Some filesystems otherwise round ctime to the same
        # instant, making post-start edits appear to belong to startup.
        time.sleep(0.01)
        self.start = time.time()
        _CODEX_EXECUTION_INPUTS.clear()
        self.addCleanup(_CODEX_EXECUTION_INPUTS.clear)

    def policy(self, **overrides):
        args = dict(runtime_version="0.162.1", model="test-model",
                    usable_window_tokens=190000, session_id="same-session",
                    home=str(self.home), execution_started_at=self.start,
                    session_created_at=self.start + 0.001,
                    execution_key=(123, "456", "same-session"))
        return codex_applied_policy(**{**args, **overrides})

    def test_later_settings_change_does_not_change_a_captured_execution(self):
        self.assertEqual(self.policy()["applied_trigger_tokens"], 100000)
        config = self.home / "config.toml"
        config.write_text("model_auto_compact_token_limit = 50000\n")
        # The test filesystem clock can lag time.time(); use a known post-start
        # mtime so a restarted observer must reject this changed input.
        os.utime(config, (self.start + 1, self.start + 1))
        self.assertEqual(self.policy()["applied_trigger_tokens"], 100000)
        _CODEX_EXECUTION_INPUTS.clear()  # a restarted observer cannot recover the old value
        self.assertEqual(self.policy()["state"], "unknown")

    def test_same_sid_in_a_new_execution_reads_its_own_startup_inputs(self):
        self.assertEqual(self.policy()["applied_trigger_tokens"], 100000)
        (self.home / "config.toml").write_text("model_auto_compact_token_limit = 50000\n")
        self.assertEqual(self.policy(execution_started_at=time.time(),
                                     session_created_at=time.time() + 0.001,
                                     execution_key=(124, "789", "same-session"))[
                                         "applied_trigger_tokens"], 50000)

    def test_late_or_missing_startup_evidence_is_unknown(self):
        self.assertEqual(self.policy(execution_started_at=self.start - 1)["state"], "unknown")
        self.assertEqual(self.policy(execution_started_at=None)["state"], "unknown")
        (self.home / "config.toml").unlink()
        self.assertEqual(self.policy()["state"], "unknown")

    def test_another_home_cannot_supply_a_missing_catalogue(self):
        other = self.home / "other"
        other.mkdir()
        (other / "config.toml").write_text("")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}):
            self.assertEqual(self.policy(home=str(other),
                                         execution_started_at=time.time())["state"], "unknown")

    def test_private_attempt_can_fetch_its_own_catalogue_during_initialization(self):
        (self.home / "config.toml").write_text("")
        # The private catalogue below is intentionally written after startup;
        # keep its ctime unambiguously later than this execution's clock.
        time.sleep(0.01)
        self.start = time.time()
        path = self.home / "models_cache.json"
        document = json.loads(path.read_text())
        document["models"][0]["auto_compact_token_limit"] = 42000
        path.write_text(json.dumps(document))
        os.utime(path, (self.start + 1, self.start + 1))
        created = self.start + 2
        self.assertEqual(self.policy(attempt_id="att-private", private_runtime_home=True,
                                     execution_key="att-private", session_created_at=created)[
                                         "applied_trigger_tokens"], 42000)
        self.assertEqual(self.policy(execution_key="another")["state"], "unknown")

    def test_an_older_rollout_cannot_supply_a_resumed_executions_version(self):
        for created in (self.start - 1, None, float("nan"), True):
            with self.subTest(created=created):
                self.assertEqual(self.policy(session_created_at=created)["state"], "unknown")

    def test_floored_usable_window_requires_an_invariant_final_trigger(self):
        # 33% admits windows 86364..86366 with three distinct native triggers.
        # 95% admits 800000..800001, both with trigger 720000. A lower configured
        # limit can also make all otherwise distinct native triggers agree.
        cases = ((33, 28500, None, None, None),
                 (37, 37001, None, None, None),
                 (95, 760000, None, 720000, None),
                 (33, 28500, 70000, 70000, None),
                 (33, 28500, 77728, None, None),
                 (100, 800000, None, 720000, 800000))
        for percent, usable, limit, trigger, window in cases:
            with self.subTest(percent=percent, usable=usable, limit=limit):
                document = json.loads((self.home / "models_cache.json").read_text())
                document["models"][0]["effective_context_window_percent"] = percent
                (self.home / "models_cache.json").write_text(json.dumps(document))
                (self.home / "config.toml").write_text(
                    "" if limit is None else f"model_auto_compact_token_limit = {limit}\n")
                self.start = time.time()
                _CODEX_EXECUTION_INPUTS.clear()
                policy = self.policy(usable_window_tokens=usable)
                self.assertEqual(policy["effective_window_tokens"], window)
                self.assertEqual(policy["applied_trigger_tokens"], trigger)
                self.assertEqual(policy["state"], "applied" if trigger else "unknown")
                if trigger is None:
                    self.assertEqual(policy["reason"],
                                     "codex-applied-trigger-not-uniquely-determined")


if __name__ == "__main__":
    unittest.main()
