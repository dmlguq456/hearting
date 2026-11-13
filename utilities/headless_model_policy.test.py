#!/usr/bin/env python3
"""Same model policy in, same decision out, on every adapter wrapper (audit §4 #8, A8).

The main-session-only refusal, the reading of an absent key and the
`--inherit-model-settings` rule live in `model_config`; each wrapper only
reads its own models.conf and names its own effort field. This file feeds the
three wrappers the same policy and expects the same answers.
"""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import model_config  # noqa: E402

# The adapter's own name for the effort a selection carries is translation.
BUDGET_FIELD = {"claude": "effort", "codex": "reasoning", "opencode": "variant"}
DECLARED = {"CFG_MAIN_SESSION_ONLY_MODELS": "solo-top"}
ABSENT: dict[str, str] = {}


def load_wrapper(harness: str):
    spec = importlib.util.spec_from_file_location(
        f"{harness}_dispatch_headless_policy", ROOT / "adapters" / harness / "bin" / "dispatch-headless.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


WRAPPERS = {harness: load_wrapper(harness) for harness in BUDGET_FIELD}


def selection(harness: str, *, model=None, inherit=False):
    return SimpleNamespace(**{
        "inherit_model_settings": inherit, "model_profile": None, "model_role": None,
        "model": model, BUDGET_FIELD[harness]: "high" if model else None,
        "registered_worker": 1, "dispatch_depth": 1, "worker_type": "owner",
        "capacity_retry": 0, "route_file": None,
    })


class HeadlessModelPolicyTest(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"AGENT_HOME": str(ROOT)})
        env.start()
        self.addCleanup(env.stop)

    def decide(self, harness: str, policy, **kwargs) -> str:
        wrapper = WRAPPERS[harness]
        patch = (mock.patch.object(wrapper, "_model_policy", side_effect=policy) if callable(policy)
                 else mock.patch.object(wrapper, "_model_policy", return_value=dict(policy)))
        with patch:
            try:
                return wrapper.resolve_model_settings(selection(harness, **kwargs))["source"]
            except wrapper.ModelSelectionError as exc:
                return exc.reason

    def receipt_state(self, harness: str, policy) -> str:
        wrapper = WRAPPERS[harness]
        patch = (mock.patch.object(wrapper, "_model_policy", side_effect=policy) if callable(policy)
                 else mock.patch.object(wrapper, "_model_policy", return_value=dict(policy)))
        with patch:
            return wrapper._main_session_only_policy_state()

    def test_a_declared_main_only_model_is_refused_everywhere(self):
        for harness in BUDGET_FIELD:
            with self.subTest(harness=harness):
                self.assertEqual(self.decide(harness, DECLARED, model="solo-top"),
                                 "headless-main-session-only-model")
                self.assertEqual(self.decide(harness, DECLARED, model="other-model"), "explicit")
                self.assertEqual(self.receipt_state(harness, DECLARED), "declared")

    def test_an_absent_key_means_no_restriction_everywhere(self):
        for harness in BUDGET_FIELD:
            with self.subTest(harness=harness):
                self.assertEqual(self.decide(harness, ABSENT, model="solo-top"), "explicit")
                self.assertEqual(self.receipt_state(harness, ABSENT), "absent")

    def test_inheritance_follows_the_declared_list_everywhere(self):
        for harness in BUDGET_FIELD:
            with self.subTest(harness=harness):
                self.assertEqual(self.decide(harness, DECLARED, inherit=True),
                                 "headless-model-inheritance-ineligible")
                self.assertEqual(self.decide(harness, ABSENT, inherit=True), "inherit")

    def test_an_unreadable_policy_refuses_the_same_way_everywhere(self):
        for harness in BUDGET_FIELD:
            wrapper = WRAPPERS[harness]

            def unavailable():
                raise wrapper.ModelSelectionError("dispatch-model-policy-unavailable", "unreadable")

            with self.subTest(harness=harness):
                self.assertEqual(self.decide(harness, unavailable, model="other-model"),
                                 "dispatch-model-policy-unavailable")
                self.assertEqual(self.receipt_state(harness, unavailable), "unavailable")

    def test_the_shared_rule_reads_the_list_not_the_adapter(self):
        self.assertIsNone(model_config.headless_model_refusal(ABSENT, "solo-top", "explicit"))
        self.assertEqual(model_config.headless_model_refusal(DECLARED, "solo-top", "explicit")[0],
                         "headless-main-session-only-model")
        self.assertIsNone(model_config.inheritance_refusal({"CFG_MAIN_SESSION_ONLY_MODELS": " "}, "Any"))
        self.assertEqual(model_config.main_session_only_state({"CFG_MAIN_SESSION_ONLY_MODELS": ""}), "declared")


if __name__ == "__main__":
    unittest.main()
