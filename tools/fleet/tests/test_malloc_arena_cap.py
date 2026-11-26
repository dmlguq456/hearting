"""Fleet caps glibc heap arenas once, before its reader threads exist."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import sys
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[2]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from fleet import fleet  # noqa: E402


class MallocArenaCapTest(unittest.TestCase):
    def test_linux_asks_for_two_arenas(self):
        calls = []
        libc = mock.Mock()
        libc.mallopt = lambda param, value: calls.append((param, value)) or 1
        with mock.patch("ctypes.CDLL", return_value=libc):
            self.assertTrue(fleet._cap_malloc_arenas(environ={}, platform="linux"))
        self.assertEqual(calls, [(-8, 2)])

    def test_operator_setting_and_other_platforms_are_left_alone(self):
        with mock.patch("ctypes.CDLL") as cdll:
            self.assertFalse(fleet._cap_malloc_arenas(
                environ={"MALLOC_ARENA_MAX": "4"}, platform="linux"))
            self.assertFalse(fleet._cap_malloc_arenas(environ={}, platform="darwin"))
        cdll.assert_not_called()

    def test_a_c_library_without_mallopt_is_not_an_error(self):
        with mock.patch("ctypes.CDLL", return_value=object()):
            self.assertFalse(fleet._cap_malloc_arenas(environ={}, platform="linux"))


if __name__ == "__main__":
    unittest.main()
