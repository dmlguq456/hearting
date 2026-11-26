#!/usr/bin/env python3
"""I-6 regression (plan-check round-1 T2): utilities/agent-home.sh (the shell
chain) and utilities/dispatch_contract.resolve_agent_home() (the python
chain) must return byte-identical strings across the same fixture matrix
(AGENT_HOME/CLAUDE_HOME set or unset x XDG `current` present or absent x
core/CORE.md present or absent at each candidate) -- this test IS the
definition of I-6: two resolvers that can silently diverge is the bug.
"""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

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
        # The one unvalidated fallback both chains converge on when nothing is
        # marked: the managed-release default path. ~/.claude is Claude Code's
        # config dir, not a harness root, since managed releases replaced the
        # ~/.claude-as-AGENT_HOME layout (2026-08-25).
        self.default = self.home / ".local" / "share" / "hearting" / "current"

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
        _mark(self.home / ".claude")  # a marked ~/.claude is no candidate either
        shell, python = self._resolve_both({"AGENT_HOME": str(target)})
        self.assertEqual(shell, python)
        self.assertEqual(shell, str(self.default))

    def test_xdg_data_home_install_wins_over_the_home_default_in_both(self) -> None:
        # GA-A5-015: with a separate XDG install and a HOME default both valid, the shell
        # chain picked the XDG one and Python the HOME one; both now read XDG_DATA_HOME.
        xdg = self.home / "xdg-data"
        _mark(xdg / "hearting" / "current")
        _mark(self.default)
        shell, python = self._resolve_both({"XDG_DATA_HOME": str(xdg)})
        self.assertEqual(shell, python)
        self.assertEqual(shell, str(xdg / "hearting" / "current"))
        shell, python = self._resolve_both({"XDG_DATA_HOME": ""})   # empty means unset, as in sh
        self.assertEqual((shell, python), (str(self.default), str(self.default)))

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

    def test_a_marked_dot_claude_is_not_a_harness_root(self) -> None:
        # Before 2026-08-25 ~/.claude was the last candidate; now a marked one
        # is skipped by both chains and the unvalidated default is returned.
        _mark(self.home / ".claude")
        shell, python = self._resolve_both({})
        self.assertEqual(shell, python)
        self.assertEqual(shell, str(self.default))

    def test_bare_environment_with_no_marked_candidate_still_agrees(self) -> None:
        """Review F-4: the previously missing matrix cell. When NO candidate
        carries core/CORE.md the shell chain returns the managed-release
        default path unvalidated; the python chain used to return _MODULE_ROOT
        here, so the two resolvers disagreed exactly where nothing else could
        catch it."""
        shell, python = self._resolve_both({})
        self.assertEqual(shell, python)
        self.assertEqual(shell, str(self.default))

    def test_bare_environment_honours_xdg_data_home_in_the_fallback(self) -> None:
        xdg = self.home / "data"
        xdg.mkdir()
        shell, python = self._resolve_both({"XDG_DATA_HOME": str(xdg)})
        self.assertEqual(shell, python)
        self.assertEqual(shell, str(xdg / "hearting" / "current"))


if __name__ == "__main__":
    unittest.main()
