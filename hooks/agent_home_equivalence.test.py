#!/usr/bin/env python3
"""I-6 regression (plan-check round-1 T2): utilities/agent-home.sh (the shell
chain) and utilities/dispatch_contract.resolve_agent_home() (the python
chain) must return byte-identical strings across the same fixture matrix
(AGENT_HOME/CLAUDE_HOME set or unset x XDG `current` present or absent x
core/CORE.md present or absent at each candidate) -- this test IS the
definition of I-6: two resolvers that can silently diverge is the bug.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_HOME_SH = ROOT / "utilities" / "agent-home.sh"
sys.path.insert(0, str(ROOT / "utilities"))
import dispatch_contract  # noqa: E402


def _mark(path: Path) -> None:
    (path / "core").mkdir(parents=True, exist_ok=True)
    (path / "core" / "CORE.md").write_text("fixture\n", encoding="utf-8")


class AgentHomeEquivalenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir()

    def _resolve_both(self, env: dict) -> tuple[str, str]:
        full_env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin"}
        full_env.update(env)
        shell = subprocess.run(
            [str(AGENT_HOME_SH)], text=True, capture_output=True, env=full_env,
        )
        self.assertEqual(shell.returncode, 0, shell.stderr)
        shell_result = shell.stdout.strip()

        import os
        prior = dict(os.environ)
        try:
            os.environ.clear()
            os.environ.update(full_env)
            python_result = str(dispatch_contract.resolve_agent_home())
        finally:
            os.environ.clear()
            os.environ.update(prior)
        return shell_result, python_result

    def test_explicit_agent_home_wins_when_marked(self) -> None:
        target = self.home / "explicit"
        _mark(target)
        shell, python = self._resolve_both({"AGENT_HOME": str(target)})
        self.assertEqual(shell, python)
        self.assertEqual(shell, str(target))

    def test_unmarked_agent_home_is_rejected_by_both(self) -> None:
        # A bare env value with no core/CORE.md must not be silently trusted
        # by either resolver -- this is the exact asymmetry this test exists
        # to close: utilities/agent-home.sh used to accept any non-empty
        # AGENT_HOME unconditionally while dispatch_contract.resolve_agent_home()
        # already validated every candidate, including AGENT_HOME.
        target = self.home / "unmarked"
        target.mkdir()
        dot_claude = self.home / ".claude"
        _mark(dot_claude)
        shell, python = self._resolve_both({"AGENT_HOME": str(target)})
        self.assertEqual(shell, python)
        self.assertEqual(shell, str(dot_claude))

    def test_claude_home_used_when_agent_home_absent(self) -> None:
        target = self.home / "claude-home"
        _mark(target)
        shell, python = self._resolve_both({"CLAUDE_HOME": str(target)})
        self.assertEqual(shell, python)
        self.assertEqual(shell, str(target))

    def test_xdg_current_wins_when_marked(self) -> None:
        xdg = self.home / ".local" / "share"
        current = xdg / "hearting" / "current"
        _mark(current)
        shell, python = self._resolve_both({"XDG_DATA_HOME": str(xdg)})
        self.assertEqual(shell, python)
        self.assertEqual(shell, str(current))

    def test_hearting_wins_when_xdg_current_unmarked(self) -> None:
        xdg = self.home / ".local" / "share"
        (xdg / "hearting" / "current").mkdir(parents=True)  # present, no marker
        hearting = self.home / "hearting"
        _mark(hearting)
        shell, python = self._resolve_both({"XDG_DATA_HOME": str(xdg)})
        self.assertEqual(shell, python)
        self.assertEqual(shell, str(hearting))

    def test_agent_setting_wins_when_hearting_unmarked(self) -> None:
        (self.home / "hearting").mkdir()  # present, no marker
        agent_setting = self.home / "agent_setting"
        _mark(agent_setting)
        shell, python = self._resolve_both({})
        self.assertEqual(shell, python)
        self.assertEqual(shell, str(agent_setting))

    def test_dot_claude_fallback_when_nothing_else_marked(self) -> None:
        dot_claude = self.home / ".claude"
        _mark(dot_claude)
        shell, python = self._resolve_both({})
        self.assertEqual(shell, python)
        self.assertEqual(shell, str(dot_claude))

    def test_bare_environment_with_no_marked_candidate_still_agrees(self) -> None:
        """Review F-4: the previously missing matrix cell. When NO candidate
        carries core/CORE.md the shell chain returns $HOME/.claude
        unvalidated; the python chain used to return _MODULE_ROOT here, so the
        two resolvers disagreed exactly where nothing else could catch it."""
        shell, python = self._resolve_both({})
        self.assertEqual(shell, python)
        self.assertEqual(shell, str(self.home / ".claude"))


if __name__ == "__main__":
    unittest.main()
