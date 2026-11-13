#!/usr/bin/env python3
"""Each adapter's capability declaration, and the shared decision that reads it."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
import unittest.mock
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import dispatch_parent_completion as P  # noqa: E402
import dispatch_pending_delivery as pending_delivery  # noqa: E402
import harness_capabilities as HC  # noqa: E402
import parent_next_directive as pnd  # noqa: E402

NATIVE_SESSION_ENV = {"claude": "CLAUDE_CODE_SESSION_ID", "codex": "CODEX_THREAD_ID",
                      "opencode": "OPENCODE_SESSION_ID"}


def direct_parent(harness, session="parent-session"):
    return SimpleNamespace(action="start", dispatch_depth=1, execution_surface="registered-headless",
                           registered_worker=True, parent_session_id=session,
                           parent_harness=harness, attempt_id="att-capability-contract")


class DeclarationTest(unittest.TestCase):
    def test_every_adapter_declares_the_same_keys(self):
        shapes = set()
        for harness in HC.HARNESSES:
            with self.subTest(harness=harness):
                declared = HC.capabilities(harness)
                self.assertEqual(declared["harness"], harness)
                shapes.add((tuple(sorted(declared)), tuple(sorted(declared["parent_completion"])),
                            tuple(sorted(declared["session_identity"]))))
        self.assertEqual(len(shapes), 1, shapes)

    def test_every_declared_carrier_is_a_delivery_the_runtime_knows(self):
        for carrier in HC.declared_carriers():
            with self.subTest(carrier=carrier):
                self.assertIn(carrier, pending_delivery.RECIPIENT_KINDS)
                self.assertEqual(pnd.parent_next(carrier, "att-x", agent_home=HC.ROOT)[0],
                                 pnd.NEXT_END_TURN)

    def test_the_access_projection_table_names_each_harness_s_means(self):
        self.assertEqual({h: HC.access(h)["enforcement"] for h in HC.HARNESSES},
                         {"claude": "tool-permission", "codex": "os-sandbox", "opencode": "tool-permission"})
        for harness in HC.HARNESSES:
            self.assertEqual(sorted(HC.access(harness)["means"]), sorted(HC.ACCESS_AXES))
        good = HC.capabilities("codex")
        for broken in ({**good, "access": {**good["access"], "enforcement": "trust"}},
                       {**good, "access": {"enforcement": "os-sandbox", "means": {"read": "x"}}}):
            with self.subTest(broken=broken["access"]), tempfile.TemporaryDirectory() as td:
                path = HC.declaration_path("codex", Path(td))
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps(broken), encoding="utf-8")
                with self.assertRaises(HC.HarnessCapabilityError):
                    HC.capabilities("codex", Path(td))

    def test_a_malformed_declaration_is_refused(self):
        good = HC.capabilities("codex")
        broken = (
            {**good, "schema_version": 2},
            {**good, "harness": "claude"},
            {**good, "parent_completion": {**good["parent_completion"], "parent_proof": "trust-me"}},
            {**good, "parent_completion": {**good["parent_completion"], "without_carrier": "maybe"}},
            {**good, "parent_completion": {**good["parent_completion"], "carrier": None}},
            {**good, "parent_completion": {k: v for k, v in good["parent_completion"].items()
                                           if k != "reason"}},
            {**good, "session_identity": {**good["session_identity"], "env": []}},
            {**good, "session_identity": {**good["session_identity"], "env": ["NOT A NAME"]}},
            {**good, "session_identity": {**good["session_identity"], "process_proof": "guess"}},
            {**good, "session_identity": {**good["session_identity"], "herdr_session_id": "maybe"}},
            {**good, "session_identity": {**good["session_identity"], "sub_session_root": "guess"}},
            {**good, "session_identity": {**good["session_identity"], "sub_session_root": "env:NOT A NAME"}},
            {k: v for k, v in good.items() if k != "session_identity"},
        )
        for value in broken:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as td:
                path = HC.declaration_path("codex", Path(td))
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(HC.HarnessCapabilityError):
                    HC.capabilities("codex", Path(td))
        with self.assertRaises(HC.HarnessCapabilityError):
            HC.capabilities("other")
        self.assertIsNone(HC.parent_completion("other")["carrier"])


class SessionIdentityDeclarationTest(unittest.TestCase):
    def test_the_identity_readers_follow_the_declarations(self):
        import session_identity
        sys.path.insert(0, str(HC.ROOT / "tools"))
        from fleet.collectors import herdr
        env = HC.session_env()
        self.assertEqual(session_identity.SESSION_ENV, env)
        self.assertEqual(set(env), set(HC.HARNESSES))
        self.assertEqual(herdr.verified_id_harnesses(), HC.herdr_verified_harnesses())
        names = [name for names in env.values() for name in names]
        # One unreadable or invalid declaration leaves only that harness unknown.
        real = HC.capabilities
        def missing_opencode(harness, root=HC.ROOT):
            if harness == "opencode":
                raise HC.HarnessCapabilityError("harness-capabilities-unreadable:opencode")
            return real(harness, root)
        with unittest.mock.patch.object(HC, "capabilities", side_effect=missing_opencode):
            self.assertEqual(set(HC.session_env()), {"claude", "codex"})
            self.assertEqual(HC.herdr_verified_harnesses(), {"claude", "codex"})
        self.assertEqual(len(names), len(set(names)), "a session variable belongs to one harness")


class ParentCompletionDecisionTest(unittest.TestCase):
    """One decision per harness, read from its declaration (no harness branch)."""

    def decide(self, harness, environ, session="parent-session"):
        request = direct_parent(harness, session)
        with mock.patch.dict(os.environ, environ, clear=True):
            request.parent_completion_delivery = P.resolve_parent_completion_delivery(request)
            try:
                P.validate_interactive_parent_launch(request)
                refused = ""
            except P.DispatchContractError as exc:
                refused = exc.reason
        return request.parent_completion_delivery, request.parent_completion_reason, refused

    @staticmethod
    def calling(harness, session):
        """The environment a tool command of `session` has in a current `harness` runtime."""
        env = {NATIVE_SESSION_ENV[harness]: session}
        declared = HC.parent_completion(harness)
        if declared["parent_proof"] == "carrier-env":
            env[HC.CARRIER_ENV] = f"{declared['carrier']}:{session}"
        return env

    def test_each_parent_gets_its_declared_carrier_or_its_declared_fallback(self):
        for harness in HC.HARNESSES:
            declared = HC.parent_completion(harness)
            own = self.calling(harness, "parent-session")
            other = self.calling(harness, "another-session")
            with self.subTest(harness=harness):
                delivery, reason, refused = self.decide(harness, own)
                if declared["carrier"]:
                    self.assertEqual((delivery, reason, refused), (declared["carrier"], declared["reason"], ""))
                else:
                    self.assertEqual((delivery, reason, refused), ("poll-fallback", "parent-identity-unmatched", ""))
                delivery, reason, refused = self.decide(harness, other)
                if declared["carrier"] and declared["parent_proof"] == "runtime-hook":
                    # The carrier binds the session itself; nothing to prove at launch.
                    self.assertEqual(delivery, declared["carrier"])
                    continue
                self.assertEqual((delivery, reason), ("poll-fallback", "parent-identity-unmatched"))
                self.assertEqual(refused, "native-parent-identity-unproven"
                                 if declared["without_carrier"] == "refuse" else "")

    def test_the_decision_follows_the_declaration_not_the_harness_name(self):
        declarations = {
            "opencode": {"carrier": "opencode-turn", "reason": "opencode-plugin-turn",
                         "parent_proof": "native-session", "without_carrier": "refuse"},
        }
        with mock.patch.object(P, "declared_parent_completion",
                               side_effect=lambda harness: declarations.get(harness)
                               or HC.parent_completion(harness)):
            self.assertEqual(self.decide("opencode", {"OPENCODE_SESSION_ID": "parent-session"}),
                             ("opencode-turn", "opencode-plugin-turn", ""))
            self.assertEqual(self.decide("opencode", {"OPENCODE_SESSION_ID": "another-session"}),
                             ("poll-fallback", "parent-identity-unmatched", "native-parent-identity-unproven"))

    def test_an_opencode_parent_is_woken_only_by_a_runtime_that_names_the_carrier(self):
        self.assertEqual(HC.parent_completion("opencode")["carrier"], "opencode-turn")
        self.assertEqual(self.decide("opencode", self.calling("opencode", "parent-session")),
                         ("opencode-turn", "opencode-plugin-turn", ""))
        self.assertEqual(pnd.parent_next("opencode-turn", "att-x", agent_home=HC.ROOT)[0], pnd.NEXT_END_TURN)
        # A server started before the carrier existed names nothing: its parent keeps the bounded wait.
        for env in ({"OPENCODE_SESSION_ID": "parent-session"},
                    {"OPENCODE_SESSION_ID": "parent-session", HC.CARRIER_ENV: ""},
                    {"OPENCODE_SESSION_ID": "parent-session", HC.CARRIER_ENV: "opencode-turn:another-session"},
                    {"OPENCODE_SESSION_ID": "parent-session", HC.CARRIER_ENV: "claude-parent-runtime:parent-session"}):
            with self.subTest(env=env):
                self.assertEqual(self.decide("opencode", env), ("poll-fallback", "parent-identity-unmatched", ""))

    def test_an_ambiguous_caller_reaches_no_session_carrier(self):
        delivery, reason, _refused = self.decide(
            "codex", {"CODEX_THREAD_ID": "parent-session", "OPENCODE_SESSION_ID": "other"})
        self.assertEqual((delivery, reason), ("poll-fallback", "parent-identity-unmatched"))


if __name__ == "__main__":
    unittest.main()
