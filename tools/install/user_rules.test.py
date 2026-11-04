"""The user's Claude user rules as one marked block for OpenCode."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import user_rules  # noqa: E402

RULE = "# 외부 소통 도구는 읽기 전용\n\n- 메일은 읽기만 한다.\n".encode()


class UserRulesBlockTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="user-rules-")
        self.addCleanup(temporary.cleanup)
        self.claude = Path(temporary.name) / ".claude"
        # Never read the real user's switch: an absent override file means on.
        switch = mock.patch.dict(os.environ, {
            "DISPATCH_DEFAULTS_CONFIG": str(Path(temporary.name) / "no-defaults.yaml")})
        switch.start()
        self.addCleanup(switch.stop)

    def write(self, relative: str, data: bytes) -> None:
        path = self.claude / "rules" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def test_no_rules_folder_or_no_rule_gives_no_block(self):
        self.assertEqual(user_rules.block(self.claude), b"")
        self.write("empty.md", b"  \n")
        self.write("notes.txt", b"not a rule\n")
        self.assertEqual(user_rules.block(self.claude), b"")

    def test_block_carries_unscoped_rules_byte_for_byte_in_path_order(self):
        self.write("z-external.md", RULE)
        self.write("team/a-style.md", b"style without a final newline")
        self.write("scoped.md", b"---\ndescription: api\npaths:\n  - src/api/**\n---\nonly for the api\n")
        self.write("bom-scoped.md", b"\xef\xbb\xbf---\npaths: src/**\n---\nalso scoped\n")
        self.write("front.md", b"---\ndescription: everywhere\n---\nkept\n")
        block = user_rules.block(self.claude)
        self.assertTrue(block.startswith(user_rules.HEADER))
        self.assertTrue(block.endswith(user_rules.END + b"\n\n"))
        body = block[len(user_rules.HEADER):block.index(user_rules.END)]
        self.assertEqual(body, b"---\ndescription: everywhere\n---\nkept\n\n"
                               b"style without a final newline\n\n" + RULE)
        self.assertNotIn(b"only for the api", block)
        self.assertNotIn(b"also scoped", block)

    def test_header_names_no_path_so_bytes_match_across_config_dirs(self):
        self.write("external.md", RULE)
        other = self.claude.parent / "elsewhere"
        (other / "rules").mkdir(parents=True)
        (other / "rules/external.md").write_bytes(RULE)
        self.assertEqual(user_rules.block(self.claude), user_rules.block(other))
        self.assertNotIn(str(self.claude).encode(), user_rules.HEADER)

    def test_a_file_that_is_not_utf8_is_skipped_with_a_note(self):
        self.write("external.md", RULE)
        self.write("legacy.md", "한글 규칙\n".encode("cp949"))
        rules, skipped = user_rules.collect(self.claude)
        self.assertEqual([name for name, _data in rules], ["external.md"])
        self.assertEqual(skipped, ["legacy.md is not UTF-8 text"])
        self.assertEqual(user_rules.block(self.claude).count(RULE), 1)

    def test_a_linked_folder_of_shared_rules_is_carried_and_a_loop_is_read_once(self):
        # Claude follows links in its rule folder and handles one that loops back.
        shared = self.claude.parent / "shared-rules"
        (shared / "nested").mkdir(parents=True)
        (shared / "team.md").write_bytes(b"shared team rule\n")
        (shared / "nested/deep.md").write_bytes(b"shared deep rule\n")
        (shared / "nested/back").symlink_to(shared, target_is_directory=True)
        self.write("external.md", RULE)
        (self.claude / "rules/shared").symlink_to(shared, target_is_directory=True)
        (self.claude / "rules/again").symlink_to(shared, target_is_directory=True)
        (self.claude / "rules/linked.md").symlink_to(shared / "team.md")
        rules, skipped = user_rules.collect(self.claude)
        names = [name for name, _data in rules]
        self.assertEqual(skipped, [])
        self.assertIn("external.md", names)
        self.assertIn("linked.md", names)
        # The shared folder is read once, under whichever link reached it first.
        shared_names = [name for name in names if name.endswith(("team.md", "deep.md"))
                        and name != "linked.md"]
        self.assertEqual(sorted(name.rsplit("/", 1)[-1] for name in shared_names),
                         ["deep.md", "team.md"])
        self.assertEqual(user_rules.block(self.claude).count(b"shared deep rule"), 1)


class UserRulesSwitchTest(unittest.TestCase):
    def test_off_in_the_user_defaults_renders_nothing(self):
        with tempfile.TemporaryDirectory(prefix="user-rules-switch-") as temporary:
            claude = Path(temporary) / ".claude"
            (claude / "rules").mkdir(parents=True)
            (claude / "rules/external.md").write_bytes(RULE)
            switch = Path(temporary) / "dispatch-defaults.yaml"
            environ = {"DISPATCH_DEFAULTS_CONFIG": str(switch)}
            self.assertTrue(user_rules.block(claude, environ))  # file absent: on
            switch.write_text("schema_version: 4\nuser_rules:\n  opencode: off\n")
            self.assertFalse(user_rules.carry_enabled(environ))
            self.assertEqual(user_rules.block(claude, environ), b"")
            switch.write_text("schema_version: 4\nuser_rules:\n  opencode: carry\n")
            self.assertEqual(user_rules.block(claude, environ).count(RULE), 1)
            switch.write_text("schema_version: 4\nuser_rules:\n  opencode: on\n")
            self.assertTrue(user_rules.carry_enabled(environ))
            switch.write_text("schema_version: 4\nuser_rules: opencode: off\n")
            self.assertFalse(user_rules.carry_enabled(environ))
            self.assertEqual(user_rules.switch_path(environ), str(switch))


if __name__ == "__main__":
    unittest.main(verbosity=2)
