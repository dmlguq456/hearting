#!/usr/bin/env python3
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import tempfile
import unittest
from pathlib import Path

P = Path(__file__).with_name("check-runtime-memory-boundary.py")
S = importlib.util.spec_from_file_location("boundary", P)
B = importlib.util.module_from_spec(S)
S.loader.exec_module(B)


class RuntimeMemoryBoundaryTest(unittest.TestCase):
    def _tree(self, files):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return root

    # regression ④: D-4 new-writer detection
    def test_current_tree_is_clean(self):
        self.assertEqual(B.find_violations(B.ROOT), [])

    def test_python_mkdir_targeting_runtime_is_rejected(self):
        root = self._tree({
            "utilities/example.py": (
                "from pathlib import Path\n"
                "def scaffold(root):\n"
                "    Path(root / '_runtime/state').mkdir(parents=True)\n"
            ),
        })
        violations = B.find_violations(root)
        self.assertTrue(violations, violations)
        self.assertIn("_runtime", violations[0])

    def test_python_write_text_targeting_notes_md_is_rejected(self):
        root = self._tree({
            "tools/example.py": (
                "def note(root):\n"
                "    (root / 'NOTES.md').write_text('hi')\n"
            ),
        })
        self.assertTrue(B.find_violations(root))

    def test_shell_mkdir_p_targeting_legacy_agent_memory_is_rejected(self):
        root = self._tree({
            "hooks/example.sh": (
                "#!/usr/bin/env sh\n"
                "mkdir -p \"$ROOT/.claude/agent-memory\"\n"
            ),
        })
        self.assertTrue(B.find_violations(root))

    def test_shell_redirect_targeting_memo_md_is_rejected(self):
        root = self._tree({
            "utilities/example.sh": (
                "#!/usr/bin/env sh\n"
                "echo hello >> \"$ROOT/memo.md\"\n"
            ),
        })
        self.assertTrue(B.find_violations(root))

    def test_read_only_mention_is_not_a_violation(self):
        root = self._tree({
            "utilities/example.py": (
                "# legacy _runtime/ is read-only; do not create it\n"
                "def read_legacy(root):\n"
                "    return (root / '_runtime').is_dir()\n"
            ),
        })
        self.assertEqual(B.find_violations(root), [])

    def test_unrelated_identifier_is_not_a_false_positive(self):
        root = self._tree({
            "utilities/example.py": (
                "def f(parent_runtime, product):\n"
                "    return parent_runtime, product.runtimes\n"
            ),
        })
        self.assertEqual(B.find_violations(root), [])

    def test_doc_prose_outside_fenced_code_is_not_scanned(self):
        root = self._tree({
            "skills/example/SKILL.md": (
                "---\nname: example\n---\n"
                "Never create `_runtime/state` or `NOTES.md` by hand.\n"
            ),
        })
        self.assertEqual(B.find_violations(root), [])

    def test_fenced_code_in_skill_doc_is_scanned(self):
        root = self._tree({
            "skills/example/SKILL.md": (
                "---\nname: example\n---\n"
                "```bash\n"
                "mkdir -p \"$ARTIFACT_ROOT/_runtime/state\"\n"
                "```\n"
            ),
        })
        self.assertTrue(B.find_violations(root))

    def test_test_fixture_files_are_never_scanned(self):
        root = self._tree({
            "hooks/example.test.py": (
                "def test_x():\n"
                "    open('NOTES.md', 'w').write('x')\n"
            ),
        })
        self.assertEqual(B.find_violations(root), [])

    def test_allowlist_exempts_only_the_recorded_signature_not_the_whole_file(self):
        # F8: the allowlist used to be a whole-file exemption -- adding a
        # *new*, different forbidden writer to an allowlisted file passed
        # clean forever. It is now a set of exact (file, sink-line) entries:
        # only a line matching a recorded signature is exempt, so a second,
        # unrecorded writer in the same file must still be caught.
        root = self._tree({
            "tools/memory/mem.py": (
                "def log(root):\n"
                "    open(root / '_runtime/events.log', 'w').write('x')\n"
                "def rogue(root):\n"
                "    open(root / 'memo.md', 'w').write('y')\n"
            ),
        })
        old_allowlist = dict(B.ALLOWLIST)
        B.ALLOWLIST["tools/memory/mem.py"] = frozenset(
            {"open(root / '_runtime/events.log', 'w').write('x')"}
        )
        try:
            violations = B.find_violations(root)
        finally:
            B.ALLOWLIST.clear(); B.ALLOWLIST.update(old_allowlist)
        self.assertTrue(violations, violations)
        self.assertTrue(any("memo.md" in row for row in violations), violations)
        self.assertFalse(any("_runtime/events.log" in row for row in violations), violations)

    # F8: a variable assigned a forbidden path on one line, then handed to a
    # write sink on a later line, used to pass clean -- the old checker only
    # matched a write primitive and the forbidden literal on the *same* line.
    def test_python_multiline_variable_bypass_is_rejected(self):
        root = self._tree({
            "tools/example.py": (
                "from pathlib import Path\n"
                "def scaffold(root):\n"
                "    target = root / '_runtime/state'\n"
                "    target.mkdir(parents=True)\n"
            ),
        })
        violations = B.find_violations(root)
        self.assertTrue(violations, violations)
        self.assertIn("target.mkdir", violations[0])

    def test_python_write_bytes_and_touch_sinks_are_rejected(self):
        root = self._tree({
            "tools/example.py": (
                "def f(root):\n"
                "    (root / 'memo.md').touch()\n"
            ),
        })
        self.assertTrue(B.find_violations(root))
        root2 = self._tree({
            "tools/example2.py": (
                "def f(root):\n"
                "    (root / '_runtime/x').write_bytes(b'x')\n"
            ),
        })
        self.assertTrue(B.find_violations(root2))

    def test_python_shutil_copy_targeting_notes_md_is_rejected(self):
        root = self._tree({
            "tools/example.py": (
                "import shutil\n"
                "def f(root, src):\n"
                "    shutil.copy(src, root / 'NOTES.md')\n"
            ),
        })
        self.assertTrue(B.find_violations(root))

    def test_shell_variable_bypass_is_rejected(self):
        root = self._tree({
            "hooks/example.sh": (
                "#!/usr/bin/env sh\n"
                "target=\"$ROOT/_runtime/state\"\n"
                "mkdir -p \"$target\"\n"
            ),
        })
        violations = B.find_violations(root)
        self.assertTrue(violations, violations)
        self.assertIn("$target", violations[0])

    def test_shell_install_and_tee_and_cp_sinks_are_rejected(self):
        for sink_line in (
            'install -d "$ROOT/_runtime/state"',
            'echo hi | tee "$ROOT/memo.md"',
            'cp source.json "$ROOT/NOTES.md"',
        ):
            with self.subTest(sink_line=sink_line):
                root = self._tree({
                    "hooks/example.sh": f"#!/usr/bin/env sh\n{sink_line}\n",
                })
                self.assertTrue(B.find_violations(root), sink_line)


if __name__ == "__main__":
    unittest.main()
