import unittest

from tools.fleet.collectors.procscan import codex_effective_cwd


class CodexArgvCwdTest(unittest.TestCase):
    def test_absolute_root_options_override_observed_cwd(self):
        observed = "/tmp/shell"
        for argv in (
            ["codex", "--cd", "/tmp/project"],
            ["codex", "-C", "/tmp/project"],
            ["codex", "--cd=/tmp/project"],
        ):
            self.assertEqual(codex_effective_cwd(argv, observed), "/tmp/project")

    def test_hidden_yolo_alias_does_not_stop_root_option_parsing(self):
        # `--yolo` is the valueless hidden alias of
        # --dangerously-bypass-approvals-and-sandbox; a cwd option after it applies.
        for argv in (
            ["codex", "--yolo", "-C", "/tmp/project"],
            ["codex", "--yolo", "--cd=/tmp/project"],
        ):
            self.assertEqual(codex_effective_cwd(argv, "/tmp/shell"), "/tmp/project")

    def test_relative_uses_launch_cwd_and_unknown_keeps_observed(self):
        self.assertEqual(codex_effective_cwd(
            ["codex", "-C", "project"], "/tmp/start/project", "/tmp/start"),
            "/tmp/start/project")
        # PWD read from /proc/<pid>/environ is the initial environment value.
        # When it agrees with the observed cwd, it still gives the base for a
        # process that has not moved since launch.
        self.assertEqual(codex_effective_cwd(
            ["codex", "-C", "project"], "/tmp/start", "/tmp/start"),
            "/tmp/start/project")
        self.assertEqual(codex_effective_cwd(
            ["codex", "-C", "project"], "/tmp/observed", None), "/tmp/observed")
        self.assertEqual(codex_effective_cwd(
            ["codex", "-C", "project"], "/tmp/unrelated", "/tmp/start"),
            "/tmp/unrelated")

    def test_value_options_are_consumed_before_root_options(self):
        self.assertEqual(codex_effective_cwd(
            ["codex", "--model", "gpt-6.1-sol", "--cd", "/tmp/model-target"],
            "/tmp/shell"), "/tmp/model-target")
        self.assertEqual(codex_effective_cwd(
            ["codex", "-c", "model=gpt-6.1-sol", "-C", "/tmp/config-target"],
            "/tmp/shell"), "/tmp/config-target")
        self.assertEqual(codex_effective_cwd(
            ["codex", "-c", "model=one", "--config=model=two", "--cd=/tmp/repeated-config"],
            "/tmp/shell"), "/tmp/repeated-config")
        self.assertEqual(codex_effective_cwd(
            ["codex", "-i", "first.png", "second.png", "--cd", "/tmp/image-target"],
            "/tmp/shell"), "/tmp/image-target")
        for option in (
                "--enable", "--disable", "--remote", "--remote-auth-token-env",
                "--local-provider", "--profile", "--sandbox", "--add-dir",
                "--ask-for-approval"):
            with self.subTest(option=option):
                self.assertEqual(codex_effective_cwd(
                    ["codex", option, "value", "--cd", "/tmp/option-target"],
                    "/tmp/shell"), "/tmp/option-target")
        self.assertEqual(codex_effective_cwd(
            ["codex", "--no-alt-screen", "--cd", "/tmp/flag-target"], "/tmp/shell"),
            "/tmp/flag-target")

    def test_attached_and_equals_value_options_are_consumed(self):
        self.assertEqual(codex_effective_cwd(
            ["codex", "--model=gpt-6.1-sol", "-C/tmp/attached-model"], "/tmp/shell"),
            "/tmp/attached-model")
        self.assertEqual(codex_effective_cwd(
            ["codex", "-cmodel=gpt-6.1-sol", "--cd=/tmp/attached-config"], "/tmp/shell"),
            "/tmp/attached-config")
        self.assertEqual(codex_effective_cwd(
            ["codex", "--image=first.png", "--cd=/tmp/image-equals"], "/tmp/shell"),
            "/tmp/image-equals")
        self.assertEqual(codex_effective_cwd(
            ["codex", "-ifirst.png", "-C/tmp/image-attached"], "/tmp/shell"),
            "/tmp/image-attached")
        self.assertEqual(codex_effective_cwd(
            ["codex", "--config", 'model="--cd /tmp/not-an-option"', "-C", "/tmp/actual"],
            "/tmp/shell"), "/tmp/actual")

    def test_unknown_option_value_does_not_become_a_cwd_option(self):
        self.assertEqual(codex_effective_cwd(
            ["codex", "--future-option", "--cd", "/tmp/wrong"], "/tmp/real"),
            "/tmp/real")

    def test_prompt_and_other_runtime_arguments_are_not_root_options(self):
        self.assertEqual(codex_effective_cwd(
            ["codex", "exec", "--", "please", "--cd", "/tmp/wrong"], "/tmp/real"),
            "/tmp/real")
        self.assertEqual(codex_effective_cwd(
            ["claude", "--cd", "/tmp/wrong"], "/tmp/real"), "/tmp/real")

    def test_prompt_subcommand_and_separator_do_not_supply_root_options(self):
        cases = (
            ["codex", "please", "--cd", "/tmp/wrong"],
            ["codex", "exec", "--cd", "/tmp/wrong"],
            ["codex", "exec", "--", "--cd", "/tmp/wrong"],
            ["codex", "--", "--cd", "/tmp/wrong"],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                self.assertEqual(codex_effective_cwd(argv, "/tmp/real"), "/tmp/real")

    def test_attached_short_and_equals_long_root_options_are_parsed(self):
        self.assertEqual(codex_effective_cwd(["codex", "-C/tmp/project"], "/tmp/shell"),
                         "/tmp/project")
        self.assertEqual(codex_effective_cwd(["codex", "--cd=/tmp/project"], "/tmp/shell"),
                         "/tmp/project")


if __name__ == "__main__":
    unittest.main()
