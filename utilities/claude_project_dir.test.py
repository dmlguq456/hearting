#!/usr/bin/env python3
"""Claude Code projects-dir encoding and the readers that depend on it."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
sys.path.insert(0, str(ROOT))

from claude_project_dir import encode_project_dir, resolve_session_transcript  # noqa: E402

# A directory name of the shape Claude Code 2.1.280 creates (206 characters): the
# 200-char cut plus `-` + base36(|java hash|) of the original cwd.
LONG_CWD = (
    "/home/user/example_project/.agent_reports/campaigns/2026-09-17_sm-relative-scoring/"
    "2026-09-18_sm-relative-veto-v2-product-replay/artifacts/experiments/"
    "2026-09-18_sm_relative_veto_v2_product_replay/raw-results"
)
LONG_NAME = (
    "-home-user-example-project--agent-reports-campaigns-2026-09-17-sm-relati"
    "ve-scoring-2026-09-18-sm-relative-veto-v2-product-replay-artifacts-exper"
    "iments-2026-09-18-sm-relative-veto-v2-product-replay-raw-iz38a"
)

# Directories whose names the old `/`, `.`, `_` rule encoded differently from
# Claude Code (or, for `.`/`_`, the old test fixture's `/`-only rule). No two
# share an encoding: the name is lossy, and a collision would test sort order.
AWKWARD = ("v2.136.1", "x_y", "a b", "c+d", "e@f", "g~h", "연구")


class EncodeProjectDirTest(unittest.TestCase):
    def test_every_non_alphanumeric_becomes_a_dash(self):
        self.assertEqual(encode_project_dir("/home/x/a.b_c"), "-home-x-a-b-c")
        self.assertEqual(encode_project_dir("/tmp/a b+c@d~e"), "-tmp-a-b-c-d-e")
        self.assertEqual(encode_project_dir("/nas/연구공유"), "-nas-----")
        self.assertEqual(encode_project_dir("/a/Z-9"), "-a-Z-9")

    def test_utf16_code_units_like_claude_code(self):
        # An astral character is two UTF-16 code units, so two dashes.
        self.assertEqual(encode_project_dir("/x/\U0001F600"), "-x---")

    def test_long_path_is_cut_and_hashed_like_claude_code(self):
        self.assertEqual(len(LONG_NAME), 206)
        self.assertEqual(encode_project_dir(LONG_CWD), LONG_NAME)
        exactly = "/" + "a" * 199
        self.assertEqual(encode_project_dir(exactly), "-" + "a" * 199)

    def test_command_line(self):
        out = subprocess.run(
            [sys.executable, str(ROOT / "utilities" / "claude_project_dir.py"), "/home/x/a.b_c 연"],
            text=True, capture_output=True, check=True,
        )
        self.assertEqual(out.stdout, "-home-x-a-b-c--\n")


class ReadersResolveAwkwardPathsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()

    def test_memory_decoder_walks_back_every_awkward_cwd(self):
        env = {"MEM_STORE": str(self.base / "store.db"), "MEM_PROJECTS": str(self.base / "projects")}
        with mock.patch.dict(os.environ, env):
            spec = importlib.util.spec_from_file_location(
                "mem_under_test", ROOT / "tools" / "memory" / "mem.py")
            mem = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mem)
        for name in AWKWARD:
            cwd = self.base / name / "repo_x.y"
            cwd.mkdir(parents=True)
            with self.subTest(name=name):
                self.assertEqual(mem._decode_enc_cwd(encode_project_dir(str(cwd))), cwd)
                # Legacy `enc_cwd` store keys still resolve.
                self.assertEqual(mem._decode_enc_cwd(mem.enc_cwd(cwd)), cwd)

    def test_fleet_collector_finds_the_transcript_claude_code_wrote(self):
        from tools.fleet.collectors import claude as collector

        home = self.base / "claude-home"
        for name in AWKWARD:
            cwd = str(self.base / name)
            transcript = home / "projects" / encode_project_dir(cwd) / "sid-1.jsonl"
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text("{}\n", encoding="utf-8")
            with self.subTest(name=name):
                self.assertEqual(
                    collector._newest_transcript_path(str(home), cwd, "sid-1"), str(transcript))

    def test_session_transcript_is_bound_to_home_and_exact_cwd(self):
        cwd = str(self.base / "repo with spaces")
        home_a, home_b = self.base / "home-a", self.base / "home-b"
        sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        for home, text in ((home_a, "worker"), (home_b, "decoy")):
            transcript = home / "projects" / encode_project_dir(cwd) / (sid + ".jsonl")
            transcript.parent.mkdir(parents=True)
            transcript.write_text(text, encoding="utf-8")
        path, reason = resolve_session_transcript(
            sid, cwd, expected_cwd=cwd, runtime_home=str(home_a))
        self.assertIsNone(reason)
        self.assertEqual(path.read_text(), "worker")
        self.assertEqual(resolve_session_transcript(
            sid, cwd, expected_cwd=str(self.base / "other"), runtime_home=str(home_a)),
            (None, "supervisor-worktree-mismatch"))
        self.assertEqual(resolve_session_transcript(
            sid, cwd, expected_cwd=cwd, runtime_home=str(self.base / "missing")), (None, None))

    def test_symlinked_worktree_finds_the_physical_project_dir(self):
        physical = self.base / "physical" / "wt"
        physical.mkdir(parents=True)
        cwd = str(self.base / "link-wt")
        os.symlink(physical, cwd, target_is_directory=True)
        home = self.base / "home"
        sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        real = home / "projects" / encode_project_dir(str(physical)) / (sid + ".jsonl")
        real.parent.mkdir(parents=True)
        real.write_text("physical", encoding="utf-8")
        path, reason = resolve_session_transcript(
            sid, cwd, expected_cwd=str(physical), runtime_home=str(home))
        self.assertIsNone(reason)
        self.assertEqual(path.read_text(), "physical")
        # The lexical project dir still wins when Claude Code named it that way.
        lexical = home / "projects" / encode_project_dir(cwd) / (sid + ".jsonl")
        lexical.parent.mkdir(parents=True)
        lexical.write_text("lexical", encoding="utf-8")
        path, _ = resolve_session_transcript(
            sid, cwd, expected_cwd=cwd, runtime_home=str(home))
        self.assertEqual(path.read_text(), "lexical")

    def test_session_transcript_rejects_path_escape(self):
        cwd = str(self.base / "repo")
        projects = self.base / "home" / "projects"
        projects.mkdir(parents=True)
        (projects / encode_project_dir(cwd)).symlink_to(self.base, target_is_directory=True)
        self.assertEqual(resolve_session_transcript(
            "../escape", cwd, expected_cwd=cwd, runtime_home=str(self.base / "home")),
            (None, "invalid-supervisor-session-event"))
        self.assertEqual(resolve_session_transcript(
            "sid", cwd, expected_cwd=cwd, runtime_home=str(self.base / "home")),
            (None, "invalid-supervisor-session-event"))


if __name__ == "__main__":
    unittest.main()
