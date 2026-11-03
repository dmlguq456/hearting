"""Explorer draws the shared session number and never mints one of its own.

Measured 2026-10-27 (Cairn report, v3.54.1): Herdr showed the Cairn Claude session as ``[39]`` and the
BC_ResNet OpenCode session (native ``ses_e88c7e169ffeY6FnJb6wuNZ0LA``) as ``[c4]``. Fleet had no native
session id for that OpenCode process, hashed its process seed to ``39``, showed ``[39] opencode`` and
dropped the real ``[39]`` Cairn row. Other OpenCode rows likewise read ``e5``/``ff`` instead of Herdr's
``ae``/``cc``. A row now carries the collector's tag, else the one shared ``resolve_tag`` rule for its
native session id, else a pending ``?n`` key no session number can equal.
"""
import importlib
import time
import unittest
from unittest import mock

from tools.fleet.explorer import live

_HANDLE = importlib.import_module(live.__name__.rsplit(".", 2)[0] + ".session_handle")

# The Herdr pane list at the same instant (/tmp/hearting-opencode-repo-identity-20261027/herdr.json).
_HERDR = {
    ("claude", "9922f71a-2d7c-4e6f-aaa9-606f83f1ffc3"): ("39", "/home/example/cairn"),
    ("opencode", "ses_e88c7e169ffeY6FnJb6wuNZ0LA"):
        ("c4", "/home/example/Project_Zoo/BC_ResNet-wt/mac-demo-simple-ui-20261026"),
    ("opencode", "ses_e89e9575cffec0fhda7G0B6IpZ"):
        ("ae", "/home/example/Project_Zoo/SR_ModelA_SD-wt/csi-booth-ui-20261026"),
    ("opencode", "ses_e876e14aaffe3VGJH5fjDwLL8E"): ("cc", "/home/example/hearting"),
}

# The measured pair: the OpenCode process Fleet saw without a native session id, and Cairn.
_OPENCODE = {"harness": "opencode", "pid": 2099088, "proc_start": "138584073", "session_id": None,
             "cwd": "/home/example/Project_Zoo/BC_ResNet-wt/mac-demo-simple-ui-20261026"}
_CLAUDE = {"harness": "claude", "pid": 2100610, "proc_start": "138585008",
           "session_id": "9922f71a-2d7c-4e6f-aaa9-606f83f1ffc3", "session_tag": "39",
           "cwd": "/home/example/cairn"}


def _shared(harness, sid, **_):
    found = _HERDR.get((harness, sid))
    return found[0] if found else None


class SharedNumberTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(_HANDLE, "resolve_tag", _shared)
        patcher.start()
        self.addCleanup(patcher.stop)

    def rows(self, sessions):
        rows, _index = live._session_records({"sessions": sessions}, time.time(), None)
        return rows

    def by_harness(self, sessions):
        return {row["harness"]: row["tag"] for row in self.rows(sessions)}

    def test_unconfirmed_row_mints_no_number(self):
        self.assertIsNone(live._tag_for(_OPENCODE, set()))

    def test_cairn_keeps_39_and_the_unconfirmed_row_stays_pending(self):
        for order in ([_OPENCODE, _CLAUDE], [_CLAUDE, _OPENCODE]):
            tags = self.by_harness(order)
            self.assertEqual(tags.get("claude"), "39")
            self.assertTrue(tags.get("opencode", "").startswith(live.PENDING_TAG))

    def test_same_native_session_wears_the_herdr_number_and_project(self):
        sessions = [{"harness": harness, "session_id": sid, "pid": 1000 + i, "proc_start": "1", "cwd": cwd}
                    for i, ((harness, sid), (_tag, cwd)) in enumerate(_HERDR.items())]
        rows = self.rows(sessions)
        shown = {row["tag"]: row["repo"] for row in rows}
        self.assertEqual(set(shown), {tag for tag, _cwd in _HERDR.values()})
        for tag, cwd in _HERDR.values():
            self.assertEqual(shown[tag], live._repo_of(None, cwd))

    def test_a_number_taken_by_another_row_is_not_reminted(self):
        # A different native session resolving to Cairn's 39 must not take it or shift to a new number.
        other = {"harness": "opencode", "session_id": "ses_other", "pid": 7, "proc_start": "1"}
        with mock.patch.object(_HANDLE, "resolve_tag", lambda h, s, **_: "39"):
            tags = [row["tag"] for row in self.rows([other, _CLAUDE])]
        self.assertEqual(tags.count("39"), 1)
        self.assertIn("39", tags)
        self.assertTrue(any(tag.startswith(live.PENDING_TAG) for tag in tags))

    def test_row_without_process_or_session_is_not_shown(self):
        self.assertEqual(self.rows([{"harness": "opencode"}]), [])


if __name__ == "__main__":
    unittest.main()
