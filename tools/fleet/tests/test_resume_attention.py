"""An interrupted resumed session remains visible while its ordinary courier waits."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import unittest
import os
from tools.fleet.config import ui_text
from tools.fleet.tests.test_explorer_live_render import _mapping,_vm,_frame,_find
from tools.fleet.model import Session
from tools.fleet import render_stages


class ResumeAttentionTest(unittest.TestCase):
    def test_pending_resume_shows_on_overview_at_narrow_and_wide_widths(self):
        data=_mapping()
        session=dict(data['sessions'][0],restart='stopped',state='idle')
        for width in (100,180):
            with self.subTest(width=width):
                frame=_frame(_vm(sessions=[session],routes=[]),cols=width)
                _find(frame,ui_text('fleet.resume.stopped'))
                self.assertTrue(all(len(row)<=width*2 for row in frame.text_rows()))

    def test_legacy_subject_uses_the_same_attention_label(self):
        session=Session('claude',os.getpid(),title='Synthetic title')
        session._resume_attention='stopped'
        self.assertIn(ui_text('fleet.resume.stopped'),render_stages._display_session_subject(session))


if __name__=='__main__':unittest.main()
