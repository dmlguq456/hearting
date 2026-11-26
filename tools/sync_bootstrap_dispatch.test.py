#!/usr/bin/env python3
"""The three bootstraps carry the shared bootstrap sources, byte for byte."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sync_bootstrap", ROOT / "tools/sync-bootstrap-dispatch.py")
S = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(S)


class BootstrapDispatchTest(unittest.TestCase):
    def test_every_bootstrap_block_is_the_source(self):
        for src, tool in S.BLOCKS:
            with self.subTest(block=src):
                body = (ROOT / src).read_text(encoding="utf-8").rstrip()
                begin, end = S._markers(src, tool)
                for target in S.TARGETS:
                    with self.subTest(target=target):
                        text = (ROOT / target).read_text(encoding="utf-8")
                        block = text.split(begin, 1)[1].split(end, 1)[0].strip("\n")
                        self.assertEqual(block, body)

    def test_a_bootstrap_without_one_marker_pair_is_refused(self):
        begin, end = S._markers(*S.BLOCKS[0])
        for text in ("no markers", f"{begin}\n{begin}\n{end}", f"{end}\n{begin}"):
            with self.subTest(text=text[:20]), self.assertRaises(ValueError):
                S.render(text, "body", "fixture", begin, end)
        self.assertEqual(S.render(f"a\n{begin}\nold\n{end}\nb", "new\n", "fixture", begin, end),
                         f"a\n{begin}\nnew\n{end}\nb")


if __name__ == "__main__":
    unittest.main()
