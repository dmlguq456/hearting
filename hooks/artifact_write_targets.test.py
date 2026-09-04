#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import artifact_write_targets as t


class TierAVerbTableTest(unittest.TestCase):
    def test_redirect_targets(self):
        for cmd, expected in [
            ("echo x > /tmp/out.txt", ["/tmp/out.txt"]),
            ("echo x >> /tmp/out.txt", ["/tmp/out.txt"]),
            ("echo x 2> /tmp/err.txt", ["/tmp/err.txt"]),
            ("echo x &> /tmp/both.txt", ["/tmp/both.txt"]),
        ]:
            with self.subTest(cmd=cmd):
                result = t.parse(cmd, Path("/tmp"))
                self.assertEqual(result["decidable"], expected)
                self.assertEqual(result["undecidable"], [])

    def test_fd_duplication_is_not_a_file_target(self):
        result = t.parse("some-cmd >/dev/null 2>&1", Path("/tmp"))
        self.assertEqual(result["decidable"], ["/dev/null"])
        self.assertEqual(result["undecidable"], [])

    def test_tee(self):
        result = t.parse("echo hi | tee /tmp/probe", Path("/tmp"))
        self.assertEqual(result["decidable"], ["/tmp/probe"])

    def test_tee_append_flag(self):
        result = t.parse("echo hi | tee -a /tmp/probe", Path("/tmp"))
        self.assertEqual(result["decidable"], ["/tmp/probe"])

    def test_cp_mv_install_ln_last_arg(self):
        for verb in ("cp", "mv", "install", "ln"):
            with self.subTest(verb=verb):
                result = t.parse(f"{verb} /tmp/a /tmp/b /tmp/out", Path("/tmp"))
                self.assertEqual(result["decidable"], ["/tmp/out"])

    def test_mkdir_touch_rm(self):
        for verb in ("mkdir -p", "touch", "rm"):
            with self.subTest(verb=verb):
                result = t.parse(f"{verb} /tmp/x", Path("/tmp"))
                self.assertEqual(result["decidable"], ["/tmp/x"])

    def test_sh_dash_c_recursion_depth_one(self):
        result = t.parse("sh -c 'echo x > /tmp/y'", Path("/tmp"))
        self.assertEqual(result["decidable"], ["/tmp/y"])

    def test_cd_tracks_relative_targets(self):
        result = t.parse("cd /tmp/sub && touch rel.txt", Path("/tmp"))
        self.assertEqual(result["decidable"], ["/tmp/sub/rel.txt"])

    def test_heredoc_redirect_target(self):
        result = t.parse("cat <<EOF > /tmp/heredoc.txt\nhello\nEOF", Path("/tmp"))
        self.assertEqual(result["decidable"], ["/tmp/heredoc.txt"])


class TierBUndecidableTest(unittest.TestCase):
    def test_dollar_variable_target(self):
        result = t.parse("echo x > $VAR/out", Path("/tmp"))
        self.assertEqual(result["decidable"], [])
        self.assertEqual(len(result["undecidable"]), 1)

    def test_command_substitution_target(self):
        result = t.parse("echo x > $(mktemp)", Path("/tmp"))
        self.assertEqual(result["decidable"], [])
        self.assertTrue(result["undecidable"])

    def test_backtick_target(self):
        result = t.parse("echo x > `mktemp`", Path("/tmp"))
        self.assertEqual(result["decidable"], [])
        self.assertTrue(result["undecidable"])

    def test_glob_target(self):
        result = t.parse("rm -rf *.log", Path("/tmp"))
        self.assertEqual(result["decidable"], [])
        self.assertTrue(result["undecidable"])

    def test_python_interpreter_write_is_undecidable(self):
        result = t.parse("python3 -c \"open('x','w')\"", Path("/tmp"))
        self.assertEqual(result["decidable"], [])
        self.assertTrue(result["undecidable"])

    def test_sed_inplace_is_undecidable(self):
        result = t.parse("sed -i s/a/b/ /tmp/file", Path("/tmp"))
        self.assertEqual(result["decidable"], [])
        self.assertTrue(result["undecidable"])

    def test_sh_dash_c_depth_two_is_undecidable(self):
        result = t.parse("sh -c \"sh -c 'echo x > /tmp/y'\"", Path("/tmp"))
        self.assertEqual(result["decidable"], [])
        self.assertTrue(any(u["reason"] == "recursion-depth-exceeded" for u in result["undecidable"]))

    def test_shell_invocation_without_literal_dash_c_is_undecidable(self):
        result = t.parse("bash /tmp/some_script.sh", Path("/tmp"))
        self.assertEqual(result["decidable"], [])
        self.assertTrue(result["undecidable"])


class LineContinuationTest(unittest.TestCase):
    """A `\\`+newline is whitespace to the shell; posix shlex leaves the newline
    glued to the next token, so the head reads as "\\ncp" / "\\npython3" and the
    whole segment used to be dropped -- no Tier A block, no Tier B record. Found
    on a real write: cairn 2026-09-03, a `cp` into a cutover-denied spec path was
    neither blocked nor observed."""

    def test_continued_tier_a_verb_is_still_seen(self):
        cmd = 'mkdir -p "$D"; \\\n' + "cp /a/prd.md /root/spec/prd.md"
        result = t.parse(cmd, Path("/tmp"))
        self.assertIn("/root/spec/prd.md", result["decidable"])

    def test_continued_redirect_is_still_seen(self):
        cmd = "echo hi; \\\n" + "echo x > /root/spec/prd.md"
        self.assertIn("/root/spec/prd.md", t.parse(cmd, Path("/tmp"))["decidable"])

    def test_continued_interpreter_is_still_observed(self):
        cmd = "AH=/x; \\\n" + "export FOO=1 \\\n" + "  BAR=2; \\\n" + "python3 /y/z.py run"
        result = t.parse(cmd, Path("/tmp"))
        self.assertTrue(result["undecidable"])
        self.assertEqual(
            [row["reason"] for row in result["undecidable"]],
            ["interpreter-mediated-write"],
        )

    def test_continuation_does_not_merge_or_invent_segments(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "mrg_probe", Path(__file__).resolve().parent / "material-route-guard.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cmd = "AH=/x; \\\n" + "export FOO=1 \\\n" + "  BAR=2; \\\n" + "python3 /y/z.py run"
        self.assertEqual(
            list(module._shell_segments(cmd)),
            [["AH=/x"], ["export", "FOO=1", "BAR=2"], ["python3", "/y/z.py", "run"]],
        )


if __name__ == "__main__":
    unittest.main()
