"""Kernel events, lifecycle invalidation and event-only Fleet display reads."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import contextlib
import copy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tools.fleet import native_events, native_refresh


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux notification backend')
class NativeEventTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.events = native_events.NativeDisplayEvents()
        self.addCleanup(self.events.stop)

    def watch(self, paths, patterns=(), processes=()):
        self.events.configure(tuple(map(str, paths)), patterns, processes)
        self.assertTrue(self.events.ready.wait(2), 'watch registration did not complete')
        self.assertTrue(self.events.take_changed(), 'post-registration read was omitted')

    def test_idle_and_unrelated_files_do_not_request_a_display_read(self):
        path = self.root / 'feed'; path.write_text('first')
        self.watch([path])
        for _ in range(1000):
            self.assertFalse(self.events.take_changed())
        (self.root / 'unrelated').write_text('not a display source')
        self.assertFalse(self.events._dirty.wait(.05))
        temporary = self.root / 'next'; temporary.write_text('second'); temporary.replace(path)
        self.assertTrue(self.events._dirty.wait(2), 'atomic source replacement was missed')
        self.assertTrue(self.events.take_changed())

    def test_rebuild_dirty_is_clearable_and_wait_changed_sees_real_events(self):
        # The producer clears the post-registration dirty after each configure so a
        # rebuild never schedules the next pass; a real write must still be seen.
        path = self.root / 'feed'; path.write_text('first')
        self.assertFalse(self.events.available)
        self.events.configure((str(path),))
        self.assertTrue(self.events.ready.wait(2))
        self.assertTrue(self.events.available)
        self.assertTrue(self.events.take_changed())          # rebuild's post-registration dirty
        self.assertFalse(self.events.take_changed())
        self.assertFalse(self.events.wait_changed(.3))      # nothing written: no wake
        built = []
        original = self.events._rebuild
        self.events._rebuild = lambda *a, **k: (built.append(1), original(*a, **k))[1]
        self.events.configure((str(path),))                  # same sources: no rebuild, no dirty
        self.assertTrue(self.events.ready.is_set())
        self.assertFalse(self.events.wait_changed(.3))
        self.assertEqual(built, [])
        path.write_text('second')
        self.assertTrue(self.events.wait_changed(2.0))       # cleared dirty does not hide events
        self.assertFalse(self.events.take_changed())

    def test_a_missing_parent_is_followed_when_it_appears(self):
        path = self.root / 'new' / 'nested' / 'feed'
        self.watch([path])
        path.parent.mkdir(parents=True); path.write_text('created')
        self.assertTrue(self.events._dirty.wait(2))
        self.assertTrue(self.events.ready.wait(2)); self.events.take_changed()
        path.write_text('later')
        self.assertTrue(self.events._dirty.wait(2), 'new parent was not watched after creation')

    def test_directory_permission_changes_invalidate_private_sources(self):
        path = self.root / 'feed'; path.write_text('value')
        self.watch([path])
        self.root.chmod(0o750)
        self.assertTrue(self.events._dirty.wait(2), 'private source boundary changed silently')

    def test_selection_replaces_watches_and_binding_notifications_are_scoped(self):
        first, second = self.root / 'first', self.root / 'second'
        first.write_text('A'); second.write_text('B')
        self.watch([first]); self.watch([second], [(str(self.root), 'session-shell-')])
        first.write_text('old selection')
        self.assertFalse(self.events._dirty.wait(.05))
        second.write_text('current selection')
        self.assertTrue(self.events._dirty.wait(2)); self.events.take_changed()
        (self.root / 'session-shell-private.json').write_text('{}')
        self.assertTrue(self.events._dirty.wait(2))

    def test_owned_process_exit_is_an_event_without_a_source_write(self):
        process = subprocess.Popen([sys.executable, '-c',
            "import sys;print('ready',flush=True);sys.stdin.buffer.read()"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd=self.root)
        def finish():
            if not process.stdin.closed:
                process.stdin.close()
            process.wait(timeout=3)
            process.stdout.close()
        self.addCleanup(finish)
        self.assertEqual(process.stdout.readline(), b'ready\n')
        birth = native_events.read_proc_start(process.pid)
        self.watch([], processes=[(process.pid, birth)])
        process.stdin.close(); process.wait(timeout=3)
        self.assertTrue(self.events._dirty.wait(2), 'process exit was not observed')
        self.events.take_changed()
        self.assertFalse(self.events._dirty.wait(.05), 'retired pidfd keeps waking the reader')

    def test_stop_is_idempotent_and_releases_only_owned_descriptors(self):
        path = self.root / 'feed'; path.write_text('value')
        self.watch([path]); self.events.stop()
        self.assertFalse(self.events._thread.is_alive())
        with (self.root / 'other').open('wb') as other:
            self.events.stop()
            other.write(b'not a retired watcher descriptor'); other.flush()

    def test_unavailable_backend_does_not_create_a_timer_fallback(self):
        with mock.patch.object(native_events.ctypes, 'CDLL', side_effect=OSError('unsupported')):
            self.watch([self.root / 'feed'])
            self.assertFalse(self.events._dirty.wait(.05))
            for _ in range(1000):
                self.assertFalse(self.events.take_changed())


class NativeDisplayLoopTest(unittest.TestCase):
    def test_idle_ui_ticks_do_not_read_sources_but_a_change_does(self):
        from types import SimpleNamespace
        from tools.fleet import render, details
        from tools.fleet.refresh import RefreshPump, LiveSnapshot, set_required_details
        from tools.fleet.explorer.runtime import ExplorerRuntime

        clock, calls, samples = [0.0], [0], {}
        class Signals:
            dirty = False
            configured = None
            def configure(self, *sources):
                if sources != self.configured:
                    self.configured, self.dirty = sources, True
            def take_changed(self):
                value, self.dirty = self.dirty, False
                return value
            def stop(self, **_kw):
                pass
        signals = Signals()
        def read(_snapshot):
            calls[0] += 1
            return ()
        def immediate_thread(**options):
            return SimpleNamespace(start=options['target'], is_alive=lambda: False,
                                   join=lambda _timeout: None)
        def pump(producer, interval, **options):
            return RefreshPump(producer, interval, clock=lambda: clock[0],
                               thread_factory=immediate_thread, **options)
        class Collector:
            def __call__(self, **_kw):
                return LiveSnapshot(sessions=[], memory={}, observations={'basic':
                    {'state':'complete', 'generation_identity':1, 'observed_at':1.0}})
            def required_detail_refresh(self, source):
                value = copy.copy(source)
                set_required_details(value, 'complete', observed_at=1.0)
                return details.DetailSnapshot(details.state_key(source), value)
        class Feed:
            def __init__(self, *_a, **_kw):
                pass
            def update(self, *_a):
                return None, False
            def stop(self):
                pass
        class Screen:
            frame = 0
            def timeout(self, _ms):
                pass
            def getmaxyx(self):
                return 50, 168
            def getch(self):
                self.frame += 1; clock[0] += .2
                if self.frame in (10, 30, 40, 70):
                    samples[self.frame] = calls[0]
                if self.frame == 31:
                    signals.dirty = True
                return ord('q') if self.frame == 80 else -1
        saved = {name:value for name,value in vars(render).items()
                 if name.startswith('_EXPLORER') or name in
                 ('_REFRESH_HEALTH', '_BLINK_ON', '_RESUME_ORDER', '_HIT_VALID', '_OFFSET')}
        self.addCleanup(lambda: [setattr(render, name, value) for name,value in saved.items()])
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            runtime = ExplorerRuntime(state_home=Path(tmp), strict_readiness=False)
            stack.enter_context(mock.patch.dict(os.environ, {'FLEET_DEMO':'1'}))
            stack.enter_context(mock.patch.multiple(render, _EXPLORER_ENABLED=True,
                _EXPLORER_RUNTIME=runtime, _PROMPT=None, _PENDING_KILL=None,
                _ExplorerFeed=Feed, RefreshPump=pump))
            for name in ('_init_colors', '_configure_input', '_draw', '_explorer_handle',
                         '_poll_pending_kill'):
                stack.enter_context(mock.patch.object(render, name))
            stack.enter_context(mock.patch.object(render.curses, 'curs_set'))
            stack.enter_context(mock.patch.object(render.os, 'write'))
            stack.enter_context(mock.patch.object(render.time, 'monotonic', side_effect=lambda: clock[0]))
            stack.enter_context(mock.patch.object(render, '_explorer_text_input', return_value=False))
            stack.enter_context(mock.patch.object(render, '_explorer_searching', return_value=False))
            stack.enter_context(mock.patch.object(native_refresh.NativeDisplayReader, 'read', side_effect=read))
            stack.enter_context(mock.patch.object(native_events, 'NativeDisplayEvents', return_value=signals))
            self.assertEqual(render._loop(Screen(), Collector(), None, 'both', 1000), 0)
        self.assertGreater(samples[10], 0)
        self.assertEqual(samples[30], samples[10], 'idle UI clock ticks started source reads')
        self.assertEqual(samples[40], samples[30] + 1, 'source event was not consumed once')
        self.assertEqual(samples[70], samples[40], 'source reads continued after the event')
