#!/usr/bin/env python3
"""Parent-cwd evidence hierarchy for the Codex headless dispatch wrapper.

Regression fixture: a managed Codex thread living in one repo dispatched from
`cd $AGENT_HOME && …`, so the launch getcwd() recorded AGENT_HOME and the job
could never nest under its parent session in Fleet (2026-07-30).
"""
import argparse
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

WH_S = importlib.util.spec_from_file_location(
    "codex_dispatch_headless", Path(__file__).with_name("dispatch-headless.py"))
WH = importlib.util.module_from_spec(WH_S)
WH_S.loader.exec_module(WH)

# Fixture ids only: the suite pins HOME/CODEX_HOME into a tmp store so it can
# never read (or depend on) the operator's real ~/.codex rollouts.
THREAD = "0199ffff-1111-7abc-8def-000000000042"


def cwd_args(**overrides):
    base = dict(parent_cwd=None, parent_session_id=None, worktree="/tmp/fixture-worktree")
    base.update(overrides)
    return argparse.Namespace(**base)


def write_rollout(sessions_root: Path, session_id: str, meta_payload, *, name=None):
    day = sessions_root / "2026" / "07" / "30"
    day.mkdir(parents=True, exist_ok=True)
    path = day / (name or f"rollout-2026-07-30T09-23-05-{session_id}.jsonl")
    lines = []
    if meta_payload is not None:
        lines.append(json.dumps({"type": "session_meta", "payload": meta_payload}))
    lines.append(json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class EffectiveParentCwd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.codex_home = self.root / "codex-home"
        self.sessions = self.codex_home / "sessions"
        self.session_cwd = self.root / "sample-note"
        self.session_cwd.mkdir(parents=True)
        self.launch_cwd = self.root / "agent-setting"
        self.launch_cwd.mkdir(parents=True)
        self.addCleanup(self.tmp.cleanup)

    def env(self, **extra):
        base = {k: v for k, v in os.environ.items()
                if k not in {"CODEX_HOME", "CODEX_SQLITE_HOME"}}
        base["HOME"] = str(self.root)
        base.update(extra)
        return base

    # (a) explicit value stays strongest — the derivation must never override it.
    def test_explicit_parent_cwd_wins_over_rollout_derivation(self):
        write_rollout(self.sessions, THREAD, {"cwd": str(self.session_cwd)})
        explicit = self.root / "explicit"
        explicit.mkdir()
        args = cwd_args(parent_cwd=str(explicit), parent_session_id=THREAD)
        with mock.patch.dict(os.environ, self.env(CODEX_HOME=str(self.codex_home)), clear=True):
            self.assertEqual(WH._effective_parent_cwd(args), os.path.realpath(explicit))

    def test_env_supplied_parent_cwd_reaches_args_and_wins(self):
        write_rollout(self.sessions, THREAD, {"cwd": str(self.session_cwd)})
        explicit = self.root / "from-env"
        explicit.mkdir()
        env = self.env(CODEX_HOME=str(self.codex_home),
                       AGENT_DISPATCH_PARENT_CWD=str(explicit))
        with mock.patch.dict(os.environ, env, clear=True):
            parsed = WH.parser().parse_known_args(
                ["--worktree", str(self.root), "--slug", "fixture",
                 "--capability", "autopilot-code"])[0]
            self.assertEqual(parsed.parent_cwd, str(explicit))
            args = cwd_args(parent_cwd=parsed.parent_cwd, parent_session_id=THREAD)
            self.assertEqual(WH._effective_parent_cwd(args), os.path.realpath(explicit))

    # (b) thread id + rollout fixture -> the session's own cwd, not the launch cwd.
    def test_thread_rollout_meta_cwd_beats_launch_cwd(self):
        write_rollout(self.sessions, THREAD, {"cwd": str(self.session_cwd)})
        args = cwd_args(parent_session_id=THREAD)
        with mock.patch.dict(os.environ, self.env(CODEX_HOME=str(self.codex_home)), clear=True), \
                mock.patch.object(WH.os, "getcwd", return_value=str(self.launch_cwd)):
            self.assertEqual(WH._effective_parent_cwd(args), os.path.realpath(self.session_cwd))

    def test_sqlite_home_is_searched_before_codex_home(self):
        other = self.root / "other-home"
        write_rollout(other / "sessions", THREAD, {"cwd": str(self.session_cwd)})
        write_rollout(self.sessions, THREAD, {"cwd": str(self.launch_cwd)})
        args = cwd_args(parent_session_id=THREAD)
        env = self.env(CODEX_SQLITE_HOME=str(other), CODEX_HOME=str(self.codex_home))
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(WH.os, "getcwd", return_value=str(self.launch_cwd)):
            self.assertEqual(WH._effective_parent_cwd(args), os.path.realpath(self.session_cwd))

    # (c) every unresolvable rollout falls through to the pre-existing getcwd tier.
    def test_unresolvable_rollout_falls_through_to_launch_cwd(self):
        launch = str(self.launch_cwd)
        cases = {
            "no-rollout": lambda: None,
            "no-session-meta": lambda: write_rollout(self.sessions, THREAD, None),
            "meta-without-cwd": lambda: write_rollout(
                self.sessions, THREAD, {"originator": "codex-tui"}),
            "cwd-path-gone": lambda: write_rollout(
                self.sessions, THREAD, {"cwd": str(self.root / "vanished")}),
            "ambiguous-rollout": lambda: [
                write_rollout(self.sessions, THREAD, {"cwd": str(self.session_cwd)},
                              name=f"rollout-2026-07-30T09-2{i}-00-{THREAD}.jsonl")
                for i in (1, 2)],
            "corrupt-first-line": lambda: (self.sessions / "2026/07/30").mkdir(
                parents=True, exist_ok=True) or (
                self.sessions / f"2026/07/30/rollout-x-{THREAD}.jsonl").write_text(
                    "{not json\n", encoding="utf-8"),
        }
        for label, setup in cases.items():
            with self.subTest(label):
                for stale in self.sessions.rglob("*.jsonl"):
                    stale.unlink()
                setup()
                args = cwd_args(parent_session_id=THREAD)
                with mock.patch.dict(os.environ, self.env(CODEX_HOME=str(self.codex_home)), clear=True), \
                        mock.patch.object(WH.os, "getcwd", return_value=launch):
                    self.assertEqual(WH._effective_parent_cwd(args), os.path.realpath(launch))

    def test_absent_or_malformed_session_id_never_touches_the_store(self):
        for sid in (None, "", "main", "not-a-uuid", "../../etc"):
            with self.subTest(repr(sid)):
                args = cwd_args(parent_session_id=sid)
                with mock.patch.dict(os.environ, self.env(CODEX_HOME=str(self.codex_home)), clear=True), \
                        mock.patch.object(WH.os, "getcwd", return_value=str(self.launch_cwd)):
                    self.assertEqual(WH._effective_parent_cwd(args),
                                     os.path.realpath(self.launch_cwd))

    # (4) a non-Codex parent has no thread rollout: behaviour is bit-identical.
    def test_cross_harness_parent_keeps_legacy_behaviour(self):
        claude_sid = "3be8e9d7-756f-4943-8fd7-24c9bcad10ac"
        write_rollout(self.sessions, THREAD, {"cwd": str(self.session_cwd)})
        args = cwd_args(parent_session_id=claude_sid)
        with mock.patch.dict(os.environ, self.env(CODEX_HOME=str(self.codex_home)), clear=True), \
                mock.patch.object(WH.os, "getcwd", return_value=str(self.launch_cwd)):
            self.assertEqual(WH._effective_parent_cwd(args), os.path.realpath(self.launch_cwd))

    # (d) worktree back-map regression.
    def test_worktree_ancestry_cannot_replace_missing_parent_evidence(self):
        primary = self.root / "repo"
        linked = self.root / "repo-wt" / "slug"
        linked.mkdir(parents=True)
        args = cwd_args(parent_session_id=THREAD, worktree=str(linked))
        porcelain = f"worktree {primary}\nHEAD abc\n\nworktree {linked}\nHEAD def\n"
        with mock.patch.dict(os.environ, self.env(CODEX_HOME=str(self.codex_home)), clear=True), \
                mock.patch.object(WH.os, "getcwd", return_value=str(linked)), \
                mock.patch.object(WH.subprocess, "check_output", return_value=porcelain):
            self.assertEqual(WH._effective_parent_cwd(args), os.path.realpath(linked))

    def test_worktree_back_map_is_skipped_when_rollout_resolves(self):
        linked = self.root / "repo-wt" / "slug"
        linked.mkdir(parents=True)
        write_rollout(self.sessions, THREAD, {"cwd": str(self.session_cwd)})
        args = cwd_args(parent_session_id=THREAD, worktree=str(linked))
        with mock.patch.dict(os.environ, self.env(CODEX_HOME=str(self.codex_home)), clear=True), \
                mock.patch.object(WH.os, "getcwd", return_value=str(linked)), \
                mock.patch.object(WH.subprocess, "check_output",
                                  side_effect=AssertionError("must not shell out")):
            self.assertEqual(WH._effective_parent_cwd(args), os.path.realpath(self.session_cwd))

    def test_unreadable_worktree_argument_still_returns_launch_cwd(self):
        args = cwd_args(parent_session_id=THREAD, worktree=None)
        with mock.patch.dict(os.environ, self.env(CODEX_HOME=str(self.codex_home)), clear=True), \
                mock.patch.object(WH.os, "getcwd", return_value=str(self.launch_cwd)):
            self.assertEqual(WH._effective_parent_cwd(args), os.path.realpath(self.launch_cwd))


if __name__ == "__main__":
    unittest.main()
