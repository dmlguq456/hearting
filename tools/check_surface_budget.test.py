#!/usr/bin/env python3
"""Regressions for the model-visible surface budget gate.

Every rule the gate claims is proven by mutation here: a paragraph added
without a cut fails, a rule added fails, a stale/missing row fails, a raise
without a reason is refused, and a reduction reseals downward.
"""
from __future__ import annotations

import importlib.util
import io
import fcntl
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "check-surface-budget.py"
BOUNDARY = ROOT / "tools" / "check-adaptation-boundary.sh"

# `tools/adaptation-guard.test.sh` rewrites `adapters/claude/CLAUDE.md` -- one
# of the budgeted surfaces -- and the boundary script's neighbours while it
# proves the guard reddens. Measuring bytes or reading that script mid-rewrite
# gives an answer about a file that was briefly not the repository's. Hold the
# shared worktree lock for the suite; tools/worktree-lock.sh owns the path.
def _worktree_lock_path() -> Path:
    try:
        common = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        common = ""
    if not common:
        return Path("/tmp/hearting-worktree-mutation.lock")
    directory = Path(common)
    if not directory.is_absolute():
        directory = ROOT / directory
    return directory / "hearting-worktree-mutation.lock"


def setUpModule() -> None:  # noqa: N802 - unittest hook
    global _LOCK_HANDLE
    try:
        _LOCK_HANDLE = open(_worktree_lock_path(), "a+", encoding="utf-8")
        fcntl.flock(_LOCK_HANDLE, fcntl.LOCK_EX)
    except OSError:
        _LOCK_HANDLE = None


def tearDownModule() -> None:  # noqa: N802 - unittest hook
    global _LOCK_HANDLE
    if _LOCK_HANDLE is not None:
        try:
            fcntl.flock(_LOCK_HANDLE, fcntl.LOCK_UN)
        finally:
            _LOCK_HANDLE.close()
            _LOCK_HANDLE = None


_LOCK_HANDLE = None

spec = importlib.util.spec_from_file_location("check_surface_budget", TOOL)
csb = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(csb)


def _run(*args: str, root: Path) -> tuple[int, str]:
    with redirect_stdout(io.StringIO()) as buf:
        rc = csb.main(["--root", str(root), *args])
    return rc, buf.getvalue()


class _FixtureMixin(unittest.TestCase):
    """A throwaway root holding small stand-ins for the nine surfaces."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="surface-budget-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for rel in csb.SURFACES:
            path = self.tmp / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f"# {rel}\n\nRead this first. You must keep it short.\n\n```\nnever printed as a rule\n```\n",
                encoding="utf-8",
            )
        (self.tmp / "tools").mkdir()
        rc, out = _run("--reseal", "--commit", "fixture", root=self.tmp)
        self.assertEqual(rc, 0, out)
        self.budget = self.tmp / csb.BUDGET_FILE

    def _budget(self) -> dict:
        return json.loads(self.budget.read_text(encoding="utf-8"))

    def _write_budget(self, data: dict) -> None:
        self.budget.write_text(json.dumps(data), encoding="utf-8")

    def _append(self, rel: str, text: str) -> None:
        path = self.tmp / rel
        path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


class DirectiveCountTest(unittest.TestCase):
    def test_counts_matches_outside_fences_case_insensitively(self) -> None:
        text = "You MUST. Never. 반드시 하고 금지.\n```\nmust not count\n```\nmust\n~~~\nnever\n~~~\n"
        self.assertEqual(csb.count_directives(text), 5)

    def test_word_boundary_excludes_substrings(self) -> None:
        self.assertEqual(csb.count_directives("mustard is nevertheless fine"), 0)

    def test_mismatched_fence_marker_does_not_close_block(self) -> None:
        self.assertEqual(csb.count_directives("```\nmust\n~~~\nmust\n```\nmust\n"), 1)

    def test_shorter_fence_inside_a_longer_fence_is_content(self) -> None:
        # R1 major 1: a 4-backtick block is not closed by 3 backticks.
        self.assertEqual(csb.count_directives("````\nmust hidden\n```\nmust still hidden\n````\n"), 0)
        self.assertEqual(csb.count_directives("~~~~\nmust hidden\n~~~\nmust still hidden\n~~~~\n"), 0)

    def test_longer_closer_closes_and_trailing_text_does_not(self) -> None:
        self.assertEqual(csb.count_directives("```\nmust hidden\n````\nmust visible\n"), 1)
        self.assertEqual(csb.count_directives("```\nmust hidden\n``` not a closer\nmust hidden\n```\nmust visible\n"), 1)

    def test_opening_fence_may_carry_an_info_string_and_indent(self) -> None:
        self.assertEqual(csb.count_directives("   ```bash\nmust hidden\n   ```\nmust visible\n"), 1)

    def test_inline_code_spans_are_not_directives(self) -> None:
        # R2 major 2: `approval_policy=never` and `never-launched` are tokens.
        text = "Set `approval_policy=never`. State ``never-launched`` is typed. You must not.\n"
        self.assertEqual(csb.count_directives(text), 1)
        self.assertEqual(csb.count_directives("`unterminated never\nmust\n"), 2)

    def test_english_imperative_forms_count(self) -> None:
        text = "Always verify. You shall stop. Do not continue. Don't skip. It must. Never."
        self.assertEqual(csb.count_directives(text), 6)

    def test_korean_forms_respect_hangul_boundaries(self) -> None:
        self.assertEqual(csb.count_directives("반드시 확인하고 금지한다. 해야 한다. 해야만 한다."), 4)
        self.assertEqual(csb.count_directives("금지. 금지된다. 금지함. 금지됨."), 4)
        self.assertEqual(csb.count_directives("반드시성과 금지어와 필수금지는 셈하지 않는다"), 0)


class CheckTest(_FixtureMixin):
    def test_sealed_fixture_passes(self) -> None:
        rc, out = _run(root=self.tmp)
        self.assertEqual(rc, 0, out)
        self.assertIn("surface_budget=ok", out)
        # A seal records what the surface measures and caps it one ordinary
        # edit higher; the two are separate fields precisely so a later reseal
        # compares measurement against measurement.
        budget = self._budget()
        self.assertEqual(budget["measured"]["core/CORE.md"]["directives"], 1)
        self.assertEqual(
            budget["surfaces"]["core/CORE.md"]["directives"],
            1 + csb.HEADROOM_DIRECTIVES_MIN,
        )
        self.assertGreater(
            budget["surfaces"]["core/CORE.md"]["bytes"],
            budget["measured"]["core/CORE.md"]["bytes"],
        )

    def test_reseal_does_not_ratchet_caps_upward(self) -> None:
        # Headroom is taken from the measurement every time, never added to the
        # previous cap: resealing an unchanged tree twice must be a no-op, or
        # the budget would widen by 3% for every reseal anyone happened to run.
        first = self._budget()["surfaces"]
        rc, out = _run("--reseal", root=self.tmp)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self._budget()["surfaces"], first)
        self.assertEqual(self._budget()["history"], [])

    def test_one_added_paragraph_fails_over_bytes(self) -> None:
        self._append("core/CORE.md", "\nOne more explanatory paragraph.\n")
        rc, out = _run(root=self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL: surface-budget over-bytes core/CORE.md", out)
        self.assertIn("over-total", out)

    def test_added_rules_fail_over_directives_even_under_bytes(self) -> None:
        data = self._budget()
        cap = data["surfaces"]["core/HOOKS.md"]["directives"]
        data["surfaces"]["core/HOOKS.md"]["bytes"] += 1_000
        data["total_bytes"] += 1_000
        self._write_budget(data)
        # One past the cap, so the directive count is what fails and not bytes.
        self._append("core/HOOKS.md", "\nYou must also do this.\n" * cap)
        rc, out = _run(root=self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn(
            f"FAIL: surface-budget over-directives core/HOOKS.md: {cap + 1} > {cap}", out
        )
        self.assertNotIn("over-bytes", out)

    def test_rules_within_the_directive_headroom_pass(self) -> None:
        # The margin is the point: a rule or two may land without a reseal.
        data = self._budget()
        data["surfaces"]["core/HOOKS.md"]["bytes"] += 1_000
        data["total_bytes"] += 1_000
        self._write_budget(data)
        self._append("core/HOOKS.md", "\nYou must also do this.\n")
        rc, out = _run(root=self.tmp)
        self.assertEqual(rc, 0, out)

    def test_rule_inside_a_code_fence_is_not_a_directive(self) -> None:
        data = self._budget()
        data["surfaces"]["core/HOOKS.md"]["bytes"] += 1_000
        data["total_bytes"] += 1_000
        self._write_budget(data)
        self._append("core/HOOKS.md", "\n```\nmust must must\n```\n")
        rc, out = _run(root=self.tmp)
        self.assertEqual(rc, 0, out)

    def test_growth_paid_for_by_an_equal_cut_still_fails_per_file(self) -> None:
        # Per-file caps are the contract; the total is a second guard, not a
        # trading pool. Growing one surface fails even when another shrank.
        self._append("core/CORE.md", "\nA new paragraph.\n")
        (self.tmp / "core/MEMORY.md").write_text("# tiny\n", encoding="utf-8")
        rc, out = _run(root=self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("over-bytes core/CORE.md", out)
        self.assertNotIn("over-total", out)

    def test_missing_surface_fails(self) -> None:
        (self.tmp / "core/MEMORY.md").unlink()
        rc, out = _run(root=self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL: surface-budget missing-surface core/MEMORY.md", out)

    def test_unsealed_surface_fails(self) -> None:
        data = self._budget()
        del data["surfaces"]["core/WORKFLOW.md"]
        self._write_budget(data)
        rc, out = _run(root=self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL: surface-budget unsealed-surface core/WORKFLOW.md", out)

    def test_stale_budget_row_fails(self) -> None:
        data = self._budget()
        data["surfaces"]["core/OLD.md"] = {"bytes": 1, "directives": 0}
        self._write_budget(data)
        rc, out = _run(root=self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL: surface-budget stale-budget-row core/OLD.md", out)

    def test_caps_edited_past_the_code_ceiling_fail(self) -> None:
        data = self._budget()
        data["surfaces"]["core/OPERATIONS.md"]["bytes"] = csb.TOTAL_BYTE_CEILING + 1
        self._write_budget(data)
        rc, out = _run(root=self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("caps-exceed-ceiling", out)

    def test_total_cap_edited_past_the_code_ceiling_fails(self) -> None:
        data = self._budget()
        data["total_bytes"] = csb.TOTAL_BYTE_CEILING + 1
        self._write_budget(data)
        rc, out = _run(root=self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("total-cap-exceeds-ceiling", out)

    def test_the_rule_total_is_the_headline_and_it_bites(self) -> None:
        # 2026-09-10: bytes were the proxy, rules are the thing. The rule
        # total is reported first and enforced with the same three failure
        # classes the byte total has.
        rc, out = _run(root=self.tmp)
        self.assertEqual(rc, 0)
        rules = [line for line in out.splitlines() if line.startswith("total directives=")]
        totals = [line for line in out.splitlines() if line.startswith("total ")]
        self.assertEqual(len(rules), 1)
        self.assertTrue(totals[0].startswith("total directives="), totals)

        data = self._budget()
        data["total_directives"] = data["measured_total_directives"] - 1
        self._write_budget(data)
        rc, out = _run(root=self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("over-total-directives", out)

    def test_rule_caps_and_totals_cannot_pass_the_code_ceiling(self) -> None:
        data = self._budget()
        data["surfaces"]["core/OPERATIONS.md"]["directives"] = csb.TOTAL_DIRECTIVE_CEILING + 1
        self._write_budget(data)
        rc, out = _run(root=self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("directive-caps-exceed-ceiling", out)

        data = self._budget()
        data["total_directives"] = csb.TOTAL_DIRECTIVE_CEILING + 1
        self._write_budget(data)
        rc, out = _run(root=self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("directive-total-cap-exceeds-ceiling", out)

    def test_a_forged_rule_ceiling_echo_is_refused(self) -> None:
        data = self._budget()
        data["ceiling_directives"] = 999_999_999
        self._write_budget(data)
        rc, out = _run(root=self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("budget-unreadable", out)
        self.assertIn("ceiling_directives", out)

    def test_ceiling_echo_must_match_the_code_ceiling(self) -> None:
        # R2 minor 1: a forged ceiling_bytes echo used to pass unnoticed.
        data = self._budget()
        data["ceiling_bytes"] = 999_999_999
        self._write_budget(data)
        rc, out = _run(root=self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("budget-unreadable", out)
        self.assertIn("ceiling_bytes", out)

    def test_unreadable_budget_fails_closed(self) -> None:
        self.budget.write_text("{", encoding="utf-8")
        rc, out = _run(root=self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("budget-unreadable", out)
        self.budget.unlink()
        rc, out = _run(root=self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("budget-unreadable", out)

    def test_quiet_prints_failures_only(self) -> None:
        self._append("core/CORE.md", "\nMore.\n")
        rc, out = _run("--quiet", root=self.tmp)
        self.assertEqual(rc, 1)
        self.assertNotIn("surface=", out)
        self.assertIn("FAIL: surface-budget over-bytes", out)


class ResealTest(_FixtureMixin):
    def test_reduction_reseals_downward_and_locks_in(self) -> None:
        before = self._budget()["surfaces"]["core/CORE.md"]["bytes"]
        (self.tmp / "core/CORE.md").write_text("# small\n", encoding="utf-8")
        rc, out = _run("--reseal", root=self.tmp)
        self.assertEqual(rc, 0, out)
        after = self._budget()
        self.assertLess(after["surfaces"]["core/CORE.md"]["bytes"], before)
        self.assertEqual(after["history"], [])
        # restoring the old text is now growth, and growth fails
        (self.tmp / "core/CORE.md").write_text("# " + "x" * before + "\n", encoding="utf-8")
        rc, out = _run(root=self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("over-bytes core/CORE.md", out)

    def test_raise_without_reason_is_refused_and_leaves_budget_untouched(self) -> None:
        original = self.budget.read_text(encoding="utf-8")
        self._append("core/CORE.md", "\nGrowth.\n")
        rc, out = _run("--reseal", root=self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL: surface-budget raise-needs-reason core/CORE.md:bytes", out)
        self.assertIn("*:total_bytes", out)
        self.assertEqual(self.budget.read_text(encoding="utf-8"), original)

    def test_raise_with_reason_is_recorded_in_history(self) -> None:
        self._append("core/CORE.md", "\nYou must grow.\n")
        rc, out = _run("--reseal", "--reason", "reviewed: moved a rule in from OPERATIONS", "--commit", "abc123", root=self.tmp)
        self.assertEqual(rc, 0, out)
        data = self._budget()
        self.assertEqual(len(data["history"]), 1)
        entry = data["history"][0]
        self.assertEqual(entry["commit"], "abc123")
        self.assertEqual(entry["reason"], "reviewed: moved a rule in from OPERATIONS")
        fields = {(r["surface"], r["field"]) for r in entry["raised"]}
        self.assertEqual(fields, {
            ("core/CORE.md", "bytes"), ("core/CORE.md", "directives"),
            ("*", "total_bytes"), ("*", "total_directives"),
        })
        rc, out = _run(root=self.tmp)
        self.assertEqual(rc, 0, out)

    def test_reseal_never_seals_above_the_code_ceiling(self) -> None:
        (self.tmp / "core/OPERATIONS.md").write_bytes(b"x" * (csb.TOTAL_BYTE_CEILING + 1))
        rc, out = _run("--reseal", "--reason", "big", root=self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("over-ceiling", out)

    def test_reseal_repairs_a_stale_ceiling_echo(self) -> None:
        # Lowering TOTAL_BYTE_CEILING in code leaves the JSON echo behind;
        # check() must refuse it and reseal() must be the way out (measured
        # 2026-09-09: the first version refused both, so no reseal was possible).
        data = self._budget()
        data["ceiling_bytes"] = csb.TOTAL_BYTE_CEILING + 5
        self._write_budget(data)
        rc, out = _run(root=self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("ceiling_bytes", out)
        rc, out = _run("--reseal", root=self.tmp)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self._budget()["ceiling_bytes"], csb.TOTAL_BYTE_CEILING)
        rc, out = _run(root=self.tmp)
        self.assertEqual(rc, 0, out)

    def test_reseal_refuses_when_a_surface_is_missing(self) -> None:
        (self.tmp / "core/HOOKS.md").unlink()
        rc, out = _run("--reseal", root=self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("missing-surface core/HOOKS.md", out)


class RepositorySealTest(unittest.TestCase):
    """The checked-in budget must match the checked-in surfaces."""

    def test_repository_is_within_its_sealed_budget(self) -> None:
        rc, out = _run(root=ROOT)
        self.assertEqual(rc, 0, out)

    def test_sealed_total_matches_ceiling_or_below(self) -> None:
        data = json.loads((ROOT / csb.BUDGET_FILE).read_text(encoding="utf-8"))
        self.assertLessEqual(data["total_bytes"], csb.TOTAL_BYTE_CEILING)
        self.assertEqual(set(data["surfaces"]), set(csb.SURFACES))

    def _boundary_function_script(self) -> str:
        """The wired function, lifted verbatim, under the script's own set -eu."""
        text = BOUNDARY.read_text(encoding="utf-8")
        start = text.index("check_model_visible_surface_budget() {")
        end = text.index("\n}\n", start) + 3
        return (
            "set -eu\nfail=0\nsay() { printf '%s\\n' \"$*\"; }\n"
            "fail_msg() { say \"FAIL: $*\"; fail=1; }\n"
            + text[start:end]
            + "\ncheck_model_visible_surface_budget\necho \"fail=$fail\"\n"
        )

    def test_wired_function_reports_an_over_budget_surface_under_set_e(self) -> None:
        # Regression: a bare $(...) assignment under set -e aborted the whole
        # boundary script silently — exit 1, zero lines (2026-09-09).
        # Mutation targets this test must catch (verified in review R1):
        #   * `return 0` inserted at the top of the wired function (no-op wiring)
        #   * the set -e fix reverted to a bare assignment
        # A tool whose main() always returns 0 is caught by CheckTest instead.
        tmp = Path(tempfile.mkdtemp(prefix="surface-budget-wiring-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        for rel in csb.SURFACES:
            (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp / rel).write_text("# doc\n", encoding="utf-8")
        (tmp / "tools").mkdir()
        (tmp / "tools" / "check-surface-budget.py").symlink_to(TOOL)
        rc, out = _run("--reseal", root=tmp)
        self.assertEqual(rc, 0, out)
        (tmp / "core/CORE.md").write_text("# doc\n\nOne more paragraph.\n", encoding="utf-8")
        script = tmp / "gate.sh"
        script.write_text(self._boundary_function_script(), encoding="utf-8")
        proc = subprocess.run(["bash", str(script)], cwd=tmp, text=True, capture_output=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("FAIL: surface-budget over-bytes core/CORE.md", proc.stdout)
        self.assertIn("FAIL: model-visible surface budget exceeded", proc.stdout)
        self.assertIn("fail=1", proc.stdout)

    def test_wired_function_passes_a_sealed_root(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="surface-budget-wiring-ok-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        for rel in csb.SURFACES:
            (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp / rel).write_text("# doc\n", encoding="utf-8")
        (tmp / "tools").mkdir()
        (tmp / "tools" / "check-surface-budget.py").symlink_to(TOOL)
        _run("--reseal", root=tmp)
        script = tmp / "gate.sh"
        script.write_text(self._boundary_function_script(), encoding="utf-8")
        proc = subprocess.run(["bash", str(script)], cwd=tmp, text=True, capture_output=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("FAIL", proc.stdout)
        self.assertIn("fail=0", proc.stdout)

    def test_boundary_check_runs_the_gate(self) -> None:
        text = BOUNDARY.read_text(encoding="utf-8")
        self.assertIn("check_model_visible_surface_budget() {", text)
        self.assertIn("python3 tools/check-surface-budget.py --root . --quiet", text)
        self.assertRegex(text, r"(?m)^check_model_visible_surface_budget$")


if __name__ == "__main__":
    unittest.main()
