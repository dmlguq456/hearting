"""Fleet opens only the exact native session captured by its selected request."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.fleet import render
from tools.fleet.explorer.state import NavState, reduce
from tools.fleet.explorer.types import FocusKey


class FleetShellPanelTest(unittest.TestCase):
    def test_control_bracket_requests_the_selected_session_panel(self):
        state=NavState(scope='session:52')
        after,effects=reduce(state,{'type':'key','key':'\x1d'})
        self.assertEqual(after,state)
        self.assertEqual(effects, (('shell.panel','52'),))

    def test_panel_dispatch_uses_the_existing_native_focus_key_and_blocks_replay(self):
        from tools.fleet.explorer import focus
        native=mock.Mock(return_value={'ok':True})
        target=(FocusKey('codex','native-first'),('codex:native-first',),())
        dispatch=getattr(render,'_explorer_open_shell_panel',lambda *_:None)
        with mock.patch.object(render,'_EXPLORER_STATE',NavState(scope='session:52')), \
                mock.patch.object(render,'_explorer_requests',return_value={'session':target}), \
                mock.patch.object(focus,'utility',return_value=SimpleNamespace(open_panel=native)), \
                mock.patch.object(render,'_EXPLORER_REPLAY_ACTIVE',False):
            dispatch(('shell.panel','52'),object(),object())
            native.assert_called_once_with('codex','native-first')
            native.reset_mock()
            with mock.patch.object(render,'_EXPLORER_REPLAY_ACTIVE',True):
                dispatch(('shell.panel','52'),object(),object())
            native.assert_not_called()


if __name__=='__main__':unittest.main()
