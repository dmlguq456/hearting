#!/usr/bin/env python3
"""Census row checks against what a ``merge=union`` of the census can produce."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("check_utility_census", ROOT / "tools" / "check-utility-census.py")
census_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(census_check)

HEADER = "# census\nlist\tname\n"
BASE = HEADER + "utility-deferred\tbase.py\nshared-utility-deferred\tshared.py\n"
# Merge fixtures also carry one source tool row and the run-time __pycache__ row,
# whose directory is never created here.
TOOL_ROWS = "tool-deferred\tobsolete_tool.py\ntool-deferred\t__pycache__\n"


def git(repo: Path, *args: str) -> None:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(repo),
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    subprocess.run(["git", "-C", str(repo), *args], check=True, env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class UnionMergeTest(unittest.TestCase):
    """Two branches each pass alone; the union merge result is checked."""

    def merged(self, ours, theirs) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            git(repo, "init", "-q", "-b", "main")
            (repo / ".gitattributes").write_text("census.tsv merge=union\n")
            (repo / "utilities").mkdir()
            for name in ("base.py", "shared.py"):
                (repo / "utilities" / name).write_text("")
            (repo / "tools").mkdir()
            (repo / "tools" / "obsolete_tool.py").write_text("")
            (repo / "census.tsv").write_text(BASE + TOOL_ROWS)
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "base")
            git(repo, "branch", "theirs")
            for branch, change in (("main", ours), ("theirs", theirs)):
                git(repo, "checkout", "-q", branch)
                change(repo)
                census = (repo / "census.tsv").read_text()
                self.assertEqual(census_check.census_conflicts(census, repo / "utilities"), [], branch)
                git(repo, "add", "-A")
                git(repo, "commit", "-qm", branch)
            git(repo, "checkout", "-q", "main")
            git(repo, "merge", "-q", "--no-edit", "theirs")
            return census_check.census_conflicts((repo / "census.tsv").read_text(), repo / "utilities")

    @staticmethod
    def add(name: str, listname: str):
        def change(repo: Path) -> None:
            (repo / "utilities" / name).write_text("")
            with (repo / "census.tsv").open("a") as fh:
                fh.write(f"{listname}\t{name}\n")
        return change

    def test_independent_additions_pass(self):
        self.assertEqual(self.merged(self.add("a.py", "utility-deferred"),
                                     self.add("b.py", "shared-utility-deferred")), [])

    def test_same_name_in_two_lists_fails(self):
        problems = self.merged(self.add("new.py", "utility-deferred"),
                               self.add("new.py", "shared-utility-deferred"))
        self.assertEqual(len(problems), 1)
        self.assertIn("new.py — declared in both", problems[0])

    def test_removed_utility_against_reclassified_row_fails(self):
        def remove(repo: Path) -> None:
            (repo / "utilities" / "base.py").unlink()
            text = (repo / "census.tsv").read_text()
            (repo / "census.tsv").write_text(text.replace("utility-deferred\tbase.py\n", ""))

        def reclassify(repo: Path) -> None:
            text = (repo / "census.tsv").read_text()
            (repo / "census.tsv").write_text(text.replace("utility-deferred\tbase.py\n",
                                                          "shared-utility-deferred\tbase.py\n"))

        problems = self.merged(remove, reclassify)
        self.assertEqual(len(problems), 1)
        self.assertIn("utilities/base.py does not exist", problems[0])

    def test_removed_tool_against_crlf_row_fails(self):
        row = "tool-deferred\tobsolete_tool.py\n"

        def remove(repo: Path) -> None:
            (repo / "tools" / "obsolete_tool.py").unlink()
            text = (repo / "census.tsv").read_text()
            (repo / "census.tsv").write_text(text.replace(row, ""))

        def crlf(repo: Path) -> None:
            data = (repo / "census.tsv").read_bytes()
            (repo / "census.tsv").write_bytes(data.replace(row.encode(), row.replace("\n", "\r\n").encode()))

        problems = self.merged(remove, crlf)
        self.assertEqual(len(problems), 1)
        self.assertIn("tools/obsolete_tool.py does not exist", problems[0])


class RowRuleTest(unittest.TestCase):
    def test_repeated_identical_rows_and_comments_stay_harmless(self):
        with tempfile.TemporaryDirectory() as tmp:
            utilities = Path(tmp)
            (utilities / "base.py").write_text("")
            (utilities / "shared.py").write_text("")
            census = "# a\n# a\n" + BASE + "utility-deferred\tbase.py\n"
            self.assertEqual(census_check.census_conflicts(census, utilities), [])

    def test_absent_runtime_tool_entry_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(census_check.census_conflicts("tool-deferred\t__pycache__\n", Path(tmp) / "utilities"), [])

    def test_repository_census_has_no_contradicting_rows(self):
        census = (ROOT / "tools" / "adaptation-census.tsv").read_text(encoding="utf-8")
        self.assertEqual(census_check.census_conflicts(census, ROOT / "utilities"), [])


if __name__ == "__main__":
    unittest.main()
