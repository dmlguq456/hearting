"""The user's Claude user rules as one marked block for OpenCode."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import user_rules  # noqa: E402

RULE = "# 외부 소통 도구는 읽기 전용\n\n- 메일은 읽기만 한다.\n".encode()


class UserRulesBlockTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="user-rules-")
        self.addCleanup(temporary.cleanup)
        self.claude = Path(temporary.name) / ".claude"

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
        self.write("front.md", b"---\ndescription: everywhere\n---\nkept\n")
        block = user_rules.block(self.claude)
        self.assertTrue(block.startswith(user_rules.BEGIN))
        self.assertIn(str(self.claude / "rules").encode(), block.splitlines()[0])
        self.assertTrue(block.endswith(user_rules.END + b"\n\n"))
        body = block[block.index(b"\n") + 1:block.index(user_rules.END)]
        self.assertEqual(body, b"---\ndescription: everywhere\n---\nkept\n\n"
                               b"style without a final newline\n\n" + RULE)
        self.assertNotIn(b"only for the api", block)

    def test_strip_returns_the_bootstrap_exactly(self):
        self.write("external.md", RULE)
        bootstrap = b"# AGENTS.md - OpenCode Adapter Bootstrap\n\nbody\n"
        self.assertEqual(user_rules.strip(user_rules.block(self.claude) + bootstrap), bootstrap)
        self.assertEqual(user_rules.strip(bootstrap), bootstrap)


if __name__ == "__main__":
    unittest.main(verbosity=2)
