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
    neither blocked nor observed.

    This affects anything that dispatches on the segment HEAD (verbs,
    interpreters, and `material-route-guard`'s own `git commit` scanner).
    Redirect scanning never reads the head and was never affected."""

    def test_continued_tier_a_verb_is_still_seen(self):
        cmd = 'mkdir -p "$D"; \\\n' + "cp /a/prd.md /root/spec/prd.md"
        result = t.parse(cmd, Path("/tmp"))
        self.assertIn("/root/spec/prd.md", result["decidable"])

    def test_continued_redirect_is_still_seen(self):
        # Regression guard only: the redirect scan never looked at the segment
        # head, so a continued redirect was NOT among the writes this defect
        # dropped. Kept so the fix cannot break the case it does not fix.
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


class NewlineAndHeredocTest(unittest.TestCase):
    """An unquoted newline ends a command like `;`, and a here-document body is
    stdin text, not shell words. Before this, the second line of a multi-line
    command became operands of the first (`mkdir -p x` + newline + `ls x` wrote
    `ls`), and heredoc'd scripts or commit messages produced redirect targets
    such as `0.5` or `=` -- real `artifact-write-outside-node-scope` blocks on
    `…/ls`, `…/0.0`, `…/4` and `…/$E/metrics.jsonl` (2026-09-09..18)."""

    @staticmethod
    def segments(cmd):
        return list(t._shell_segments(cmd))

    def test_newline_separates_commands(self):
        result = t.parse("mkdir -p /r/x/eval\nls /r/x", Path("/r"))
        self.assertEqual(result["decidable"], ["/r/x/eval"])
        self.assertEqual(result["undecidable"], [])

    def test_newline_inside_quotes_stays_part_of_the_word(self):
        self.assertEqual(
            self.segments('echo "a\nb"\nls'),
            [["echo", "a\nb"], ["ls"]],
        )
        result = t.parse("printf 'one\nmkdir /r/bogus\n' > /r/q\ntouch /r/t", Path("/r"))
        self.assertEqual(result["decidable"], ["/r/q", "/r/t"])

    def test_heredoc_body_is_not_tokenized_as_shell(self):
        result = t.parse("cat <<EOF > /r/notes.txt\nratio > 0.5 means pass\nEOF", Path("/r"))
        self.assertEqual(result["decidable"], ["/r/notes.txt"])
        self.assertEqual(result["undecidable"], [])

    def test_commit_message_heredoc_invents_no_target(self):
        result = t.parse("git commit -F - <<'MSG'\nfix: a >= 4\nMSG", Path("/r"))
        self.assertEqual(result, {"decidable": [], "undecidable": []})

    def test_heredoc_body_cannot_hide_the_operator_line_write(self):
        # An apostrophe in the body used to make shlex fail on the whole
        # command, so the real write below was neither blocked nor recorded.
        result = t.parse("cat <<EOF > /r/p.md\nit's fine\nEOF", Path("/r"))
        self.assertEqual(result["decidable"], ["/r/p.md"])
        result = t.parse("cat <<EOF | tee /r/tee\nbody > 3\nEOF", Path("/r"))
        self.assertEqual(result["decidable"], ["/r/tee"])

    def test_heredoc_operator_forms(self):
        cmd = (
            "cat <<-'EOF' > /r/t\n\tx > 1\n\tEOF\n"
            'cat <<"END" >/r/u\ny > 2\nEND\n'
            "cat << \\STOP > /r/v\nz > 3\nSTOP\n"
            "cat <<A <<-B > /r/two\na > 4\nA\n\tb > 5\n\tB\n"
            "touch /r/after"
        )
        result = t.parse(cmd, Path("/r"))
        self.assertEqual(
            result["decidable"], ["/r/after", "/r/t", "/r/two", "/r/u", "/r/v"]
        )
        self.assertEqual(result["undecidable"], [])

    def test_unterminated_heredoc_keeps_the_old_tokenization(self):
        # Without a terminator line the pre-pass does not guess where shell
        # text resumes: the lines before the heredoc are split, the rest is
        # tokenized exactly as before this change.
        result = t.parse("touch /r/first\ncat <<EOF\nno terminator > 3\n", Path("/r"))
        self.assertEqual(result["decidable"], ["/r/3", "/r/first"])

    def test_arithmetic_shift_is_not_a_heredoc(self):
        result = t.parse("echo $((1<<2)) > /r/n\ntouch /r/m", Path("/r"))
        self.assertEqual(result["decidable"], ["/r/m", "/r/n"])

    def test_continuation_still_joins_across_the_newline(self):
        result = t.parse("mkdir -p /r/a \\\n  /r/b\nls /r/c", Path("/r"))
        self.assertEqual(result["decidable"], ["/r/a", "/r/b"])


class UnknownCwdTest(unittest.TestCase):
    """After a `cd` this parser cannot follow, the shell's cwd is unknown:
    relative targets are Tier B, never resolved against the old cwd."""

    def test_cd_to_undecidable_directory(self):
        result = t.parse("cd $X/sub; echo hi >> out.txt", Path("/r"))
        self.assertEqual(result["decidable"], [])
        self.assertEqual(
            [row["reason"] for row in result["undecidable"]],
            ["relative-target-after-unknown-cd"],
        )
        result = t.parse("cd `pwd`/sub; echo hi >> /r/abs.txt; touch rel", Path("/r"))
        self.assertEqual(result["decidable"], ["/r/abs.txt"])
        self.assertEqual(len(result["undecidable"]), 1)

    def test_cd_inside_a_multiline_subshell_or_conditional(self):
        for cmd in (
            "(\n  cd /r/sub\n  make\n)\ntouch out.txt",
            "if [ -d b ]; then\n  cd b\nfi\ntouch out.txt",
        ):
            with self.subTest(cmd=cmd):
                result = t.parse(cmd, Path("/r"))
                self.assertEqual(result["decidable"], [])
                self.assertEqual(
                    [row["reason"] for row in result["undecidable"]],
                    ["relative-target-after-unknown-cd"],
                )


if __name__ == "__main__":
    unittest.main()
