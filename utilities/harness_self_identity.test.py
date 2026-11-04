#!/usr/bin/env python3
"""A harness runtime names itself in its own tool commands, whichever process started it.

The incident: a Codex thread served by the shared app-server daemon that a Claude tool shell
started ran its commands with the daemon starter's Claude session id next to its own thread id.
The tool-command env is built here from each shipped adapter config (`fixtures/harness_tool_env`),
then handed to every consumer that decides identity: the shared resolver, compose's caller
default, the route-chain writer, the owner's caller check, and the peer sender.
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "fixtures"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tools" / "install"))

import dispatch_parent_completion as P  # noqa: E402
import harness_tool_env as E  # noqa: E402
from fleet import route_chain as RC  # noqa: E402

SIDS = {
    "claude": "c0c0c0c0-1111-4111-8111-aaaaaaaaaaaa",
    "codex": "d0d0d0d0-2222-4222-8222-bbbbbbbbbbbb",
    "opencode": "ses_fixture00000000000000000025",
}


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, str(HERE / filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


OWNER = _load("dispatch_owner_identity_under_test", "dispatch-owner.py")
STEWARD = _load("peer_steward_identity_under_test", "peer-steward.py")

PAIRS = [(a, b) for a in E.HARNESSES for b in E.HARNESSES if a != b]


def child_env(parent, child, stale_name=False):
    """The `child` harness's tool env when its process tree was started from a `parent` tool shell."""
    inherited = E.daemon_started_from(E.tool_shell_env(parent, {}, SIDS[parent]))
    if stale_name:
        inherited["AGENT_DISPATCH_CALLER_HARNESS"] = parent
    return E.tool_shell_env(child, inherited, SIDS[child])


class _EnvCase(unittest.TestCase):
    def tool_env(self, parent, child, **kw):
        try:
            return child_env(parent, child, **kw)
        except E.ToolMissing as exc:
            self.skipTest(str(exc))


class ToolShellIdentityMatrixTest(_EnvCase):
    """Every parent -> child pair resolves to the innermost harness and its own session."""

    def assert_resolves(self, env, child):
        sid = SIDS[child]
        self.assertEqual(P.interactive_parent_identity(env), (child, sid))
        self.assertEqual(P.default_parent_harness("claude", env), child)
        self.assertEqual(RC.writer_identity(env), (child, sid))
        self.assertEqual(OWNER._caller_harness(env), child)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(STEWARD._current_session_identity(), (sid, child))
        self.assertEqual(E.foreign_session_values(env, child), {})
        # A nested stage worker is still not the composing interactive session.
        self.assertIsNone(RC.writer_identity({**env, "AGENT_DISPATCH_DEPTH": "2"}))

    def test_every_pair_resolves_to_the_innermost_harness(self):
        for parent, child in PAIRS:
            with self.subTest(parent=parent, child=child):
                self.assert_resolves(self.tool_env(parent, child), child)

    def test_the_incident_shape_a_codex_thread_in_a_claude_started_daemon(self):
        env = self.tool_env("claude", "codex")
        self.assertEqual(env["CODEX_THREAD_ID"], SIDS["codex"])
        self.assertFalse(env.get("CLAUDE_CODE_SESSION_ID"))
        self.assert_resolves(env, "codex")

    def test_a_stale_explicit_name_from_the_parent_never_survives(self):
        for parent, child in PAIRS:
            with self.subTest(parent=parent, child=child):
                self.assert_resolves(self.tool_env(parent, child, stale_name=True), child)

    def test_a_three_level_chain_still_names_the_innermost_harness(self):
        try:
            first = E.tool_shell_env("codex", {}, SIDS["codex"])
            second = E.tool_shell_env("claude", E.daemon_started_from(first), SIDS["claude"])
            final = E.tool_shell_env("codex", E.daemon_started_from(second), "e0e0e0e0-3333-4333-8333-cccccccccccc")
        except E.ToolMissing as exc:
            self.skipTest(str(exc))
        self.assertEqual(P.interactive_parent_identity(final),
                         ("codex", "e0e0e0e0-3333-4333-8333-cccccccccccc"))
        self.assertEqual(RC.writer_identity(final), ("codex", "e0e0e0e0-3333-4333-8333-cccccccccccc"))


class AdapterDeclarationTest(unittest.TestCase):
    """Each adapter's shipped config clears inherited caller names and the other harnesses' ids.

    No adapter exports a harness NAME: a name exported to a tool shell is inherited by whatever that
    shell starts (the shared Codex daemon) and would then be wrong for every grandchild.
    """

    def test_claude_settings_env_blanks_the_caller_name_and_foreign_ids(self):
        env = E.claude_env_declaration()
        for key in ("AGENT_DISPATCH_CALLER_HARNESS", "CODEX_THREAD_ID", "CODEX_SESSION_ID",
                    "OPENCODE_SESSION_ID"):
            self.assertEqual(env.get(key), "", key)
        self.assertNotIn("AGENT_DISPATCH_CURRENT_HARNESS", env)

    def test_claude_managed_env_keys_match_the_settings_env(self):
        import runtime_activation
        self.assertEqual(set(E.claude_env_declaration()),
                         set(runtime_activation.CLAUDE_MANAGED_ENV_KEYS))

    def test_codex_fragment_sets_nothing_and_excludes_the_caller_name_and_foreign_ids(self):
        policy = E.codex_policy()
        self.assertEqual(policy["set"], {})
        for key in ("AGENT_DISPATCH_CALLER_HARNESS", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID",
                    "OPENCODE_SESSION_ID", "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION"):
            self.assertIn(key, policy["exclude"])
        self.assertNotIn("AGENT_DISPATCH_CURRENT_HARNESS", policy["exclude"])

    def test_opencode_shell_env_blanks_the_caller_name_and_foreign_ids(self):
        try:
            with_sid = E.opencode_shell_env("ses_abc")
            without_sid = E.opencode_shell_env(None)
        except E.ToolMissing as exc:
            self.skipTest(str(exc))
        for env in (with_sid, without_sid):
            for key in ("AGENT_DISPATCH_CALLER_HARNESS", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID",
                        "CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION"):
                self.assertEqual(env.get(key), "", key)
            self.assertNotIn("AGENT_DISPATCH_CURRENT_HARNESS", env)
        self.assertEqual(with_sid.get("OPENCODE_SESSION_ID"), "ses_abc")
        self.assertNotIn("OPENCODE_SESSION_ID", without_sid)

    def test_no_config_surface_touches_the_worker_marker(self):
        # CURRENT_HARNESS marks agents and workers (actor by=agent, nested eligibility); a registered
        # worker keeps it through every adapter's clearing.
        self.assertNotIn("AGENT_DISPATCH_CURRENT_HARNESS", E.claude_env_declaration())
        self.assertNotIn("AGENT_DISPATCH_CURRENT_HARNESS", E.codex_policy()["exclude"])


class UnchangedResolverContractsTest(unittest.TestCase):
    """The resolvers keep refusing an unnamed mixed env and an invalid name."""

    MIXED = {"CLAUDE_CODE_SESSION_ID": SIDS["claude"], "CODEX_THREAD_ID": SIDS["codex"]}

    def test_an_unnamed_mixed_env_is_ambiguous(self):
        with self.assertRaises(P.DispatchContractError) as caught:
            P.interactive_parent_identity(dict(self.MIXED))
        self.assertEqual(caught.exception.reason, "caller-harness-ambiguous")
        self.assertIsNone(RC.writer_identity(dict(self.MIXED)))

    def test_an_invalid_name_is_refused(self):
        env = {**self.MIXED, "AGENT_DISPATCH_CALLER_HARNESS": "gemini"}
        with self.assertRaises(P.DispatchContractError) as caught:
            P.interactive_parent_identity(env)
        self.assertEqual(caught.exception.reason, "caller-harness-invalid")

    def test_an_explicit_name_is_followed_in_a_mixed_env(self):
        env = {**self.MIXED, "AGENT_DISPATCH_CALLER_HARNESS": "codex"}
        self.assertEqual(P.interactive_parent_identity(env), ("codex", SIDS["codex"]))
        self.assertEqual(RC.writer_identity(env), ("codex", SIDS["codex"]))


class PartialRolloutTest(_EnvCase):
    """A host where only some harnesses carry their config never mis-names a harness.

    The worst case is today's ambiguity (refuse / no identity), never the wrong harness.
    """

    def assert_unresolved(self, env):
        with self.assertRaises(P.DispatchContractError) as caught:
            P.interactive_parent_identity(env)
        self.assertEqual(caught.exception.reason, "caller-harness-ambiguous")
        self.assertIsNone(RC.writer_identity(env))
        with self.assertRaises(OWNER.OwnerError):
            OWNER._caller_harness(env)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(STEWARD._current_session_identity(), ("", "unknown"))

    def started_from(self, parent, parent_installed, child, child_installed):
        try:
            inherited = E.daemon_started_from(
                E.tool_shell_env(parent, {}, SIDS[parent], installed=parent_installed))
            return E.tool_shell_env(child, inherited, SIDS[child], installed=child_installed)
        except E.ToolMissing as exc:
            self.skipTest(str(exc))

    def test_claude_env_installed_codex_block_missing_never_resolves_claude(self):
        env = self.started_from("claude", True, "codex", False)
        self.assertEqual(env["CODEX_THREAD_ID"], SIDS["codex"])
        self.assertTrue(env["CLAUDE_CODE_SESSION_ID"])
        self.assertFalse(env.get("AGENT_DISPATCH_CALLER_HARNESS"))
        self.assert_unresolved(env)

    def test_codex_block_installed_claude_env_missing_never_resolves_codex(self):
        env = self.started_from("codex", True, "claude", False)
        self.assertTrue(env["CODEX_THREAD_ID"])
        self.assertEqual(env["CLAUDE_CODE_SESSION_ID"], SIDS["claude"])
        self.assertFalse(env.get("AGENT_DISPATCH_CALLER_HARNESS"))
        self.assert_unresolved(env)

    def test_opencode_plugin_installed_other_harness_config_missing_never_resolves_opencode(self):
        for child in ("claude", "codex"):
            with self.subTest(child=child):
                env = self.started_from("opencode", True, child, False)
                self.assertTrue(env["OPENCODE_SESSION_ID"])
                self.assertFalse(env.get("AGENT_DISPATCH_CALLER_HARNESS"))
                self.assert_unresolved(env)

    def test_other_harness_config_installed_opencode_plugin_installed_resolves_the_child(self):
        # Both halves present for the pair: the matrix result still holds when only these two
        # of the three configs are applied.
        for parent, child in (("claude", "opencode"), ("codex", "opencode"),
                              ("opencode", "claude"), ("opencode", "codex")):
            with self.subTest(parent=parent, child=child):
                env = self.started_from(parent, True, child, True)
                self.assertEqual(P.interactive_parent_identity(env), (child, SIDS[child]))

    def test_a_per_command_override_in_a_contaminated_shell_is_honored(self):
        env = self.started_from("claude", True, "codex", False)
        self.assert_unresolved(env)
        self.assertEqual(P.interactive_parent_identity({**env, "AGENT_DISPATCH_CALLER_HARNESS": "codex"}),
                         ("codex", SIDS["codex"]))
        self.assertEqual(RC.writer_identity({**env, "AGENT_DISPATCH_CALLER_HARNESS": "codex"}),
                         ("codex", SIDS["codex"]))
        with mock.patch.dict(os.environ, {**env, "AGENT_DISPATCH_CALLER_HARNESS": "codex"}, clear=True):
            self.assertEqual(STEWARD._current_session_identity(), (SIDS["codex"], "codex"))


class RegisteredWorkerIdentityTest(_EnvCase):
    """A registered worker keeps resolving to its own harness once its adapter clears the caller name.

    The dispatch wrapper exports CURRENT and CALLER (`worker_runtime_identity`); the adapter then blanks
    CALLER but not the CURRENT worker marker, which the resolvers read when CALLER is blank.
    """

    def test_every_harness_worker_resolves_to_itself_after_clearing(self):
        for harness in E.HARNESSES:
            for parent in E.HARNESSES:
                with self.subTest(harness=harness, parent=parent):
                    try:
                        inherited = E.tool_shell_env(parent, {}, SIDS[parent])
                        wrapper_env = {**inherited, **P.worker_runtime_identity(harness)}
                        env = E.tool_shell_env(harness, wrapper_env, SIDS[harness])
                    except E.ToolMissing as exc:
                        self.skipTest(str(exc))
                    self.assertFalse(env.get("AGENT_DISPATCH_CALLER_HARNESS"))
                    self.assertEqual(env["AGENT_DISPATCH_CURRENT_HARNESS"], harness)
                    self.assertEqual(P.interactive_parent_identity(env), (harness, SIDS[harness]))
                    self.assertEqual(OWNER._caller_harness(env), harness)

    def test_a_worker_without_a_native_id_still_resolves_through_the_worker_marker(self):
        for harness in E.HARNESSES:
            with self.subTest(harness=harness):
                env = {"AGENT_DISPATCH_CALLER_HARNESS": "", **{"AGENT_DISPATCH_CURRENT_HARNESS": harness}}
                self.assertEqual(P.interactive_parent_identity(env)[0], harness)


class PeerSenderLedgerTest(_EnvCase):
    """The peer ledger row names the sender with the same identity the resolvers use."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "jobs.log").touch()

    def _record(self, env):
        env = {**env, "AGENT_DISPATCH_JOBS": str(self.root / "jobs.log"),
               "AGENT_PEER_LEDGER_ROOT": str(self.root), "HOME": str(self.root / "home"),
               "FLEET_SESSION_REGISTRY_DIR": str(self.root / "registry")}
        with mock.patch.dict(os.environ, env, clear=True):
            STEWARD._record(to_harness="codex", to_name="peer-x", kind="steer", summary_text="hi")
        rows = []
        for path in (self.root / "peer-messages").rglob("*.jsonl"):
            rows += [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def test_a_codex_thread_in_a_claude_started_daemon_is_recorded_as_codex(self):
        row = self._record(self.tool_env("claude", "codex"))
        self.assertEqual(row["from"]["harness"], "codex")
        self.assertEqual(row["from"]["session_id"], SIDS["codex"])
        self.assertNotEqual(row["from"].get("name"), "claude")

    def test_an_unnamed_mixed_env_is_recorded_with_an_unknown_sender(self):
        row = self._record(dict(UnchangedResolverContractsTest.MIXED))
        self.assertEqual(row["from"]["harness"], "unknown")
        self.assertFalse(row["from"].get("session_id"))


if __name__ == "__main__":
    unittest.main()
