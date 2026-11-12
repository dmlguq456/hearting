"""Exact NOW joins and hermetic real-loop terminal bursts (no live state)."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import copy
import curses
import fcntl
import json
import os
from pathlib import Path
import pty
import selectors
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from unittest import mock

TOOLS = Path(__file__).resolve().parents[2]
if len(sys.argv)>1 and sys.argv[1]=='--child':
    TOOLS = Path(sys.argv[2]) / 'tools'
sys.path.insert(0, str(TOOLS))
from fleet import details, model, projection, render
from fleet.refresh import LiveSnapshot, RefreshPump


def now(row):
    return render._plain(render._context_detail_row(row, term_width=168)[0])


class NowTest(unittest.TestCase):
    def rows(self):
        for harness in ('claude', 'codex', 'opencode'):
            for role in ('owner', 'worker'):
                row = model.DispatchJob(key='code', harness=harness, worker_type=role,
                    attempt_id='att-fixture', pid=9900001, proc_start='100',
                    cwd='/fixture', route_id='rt-fixture', route_hash='hash', liveness='working')
                row._detail_activity_key = ('log-v1',)
                projection.attach_projections([], [row], fast_first=True)
                yield row

    def test_pending_summary_command_and_successful_empty(self):
        for row in self.rows():
            with self.subTest(harness=row.harness, role=row.worker_type):
                self.assertIn('loading', now(row))
                row.summary = 'current summary'
                self.assertNotIn('loading', now(row))
                row.summary = None
                row.exec_tool = {'name': 'python fixture.py'}
                self.assertNotIn('loading', now(row))
                row.exec_tool = None
                source = LiveSnapshot(jobs=[row])
                detail = details.DetailSnapshot(details.state_key(source), copy.deepcopy(source))
                merged = details.merge(source, detail).jobs[0]
                self.assertIn('no summary yet', now(merged))
                self.assertNotIn('loading', now(merged))
                self.assertIn('loading', now(row))  # immutable basic observation

    def test_detail_read_advances_activity_without_losing_its_only_now(self):
        for row in self.rows():
            source = LiveSnapshot(jobs=[row])
            detail = details.DetailSnapshot(details.state_key(source), copy.deepcopy(source))
            old = detail.snapshot.jobs[0]
            old._detail_activity_key = ('log-v2',)
            old.exec_tool = {'name': 'native current command'}
            old.summary = 'bounded summary'
            merged = details.merge(source, detail).jobs[0]
            self.assertEqual(merged.exec_tool, old.exec_tool)
            self.assertIn('bounded summary', now(merged))
            row._detail_activity_key = ('log-v3',)
            merged = details.merge(source, detail).jobs[0]
            self.assertIsNone(merged.exec_tool)
            self.assertIn('loading', now(merged))
            row.summary = 'new basic summary'
            row.exec_tool = {'name': 'new basic command'}
            merged = details.merge(source, detail).jobs[0]
            self.assertEqual(merged.exec_tool, row.exec_tool)
            self.assertNotIn('bounded summary', now(merged))
            self.assertNotIn('loading', now(merged))

    def test_identity_and_route_changes_reject_delayed_now(self):
        for row in self.rows():
            source = LiveSnapshot(jobs=[row])
            detail = details.DetailSnapshot(details.state_key(source), copy.deepcopy(source))
            detail.snapshot.jobs[0].summary = 'foreign NOW'
            for field in ('pid', 'proc_start', 'attempt_id', 'route_id', 'route_hash',
                          'route_file', '_runtime_session_id', 'harness'):
                changed = copy.deepcopy(source)
                setattr(changed.jobs[0], field, 'foreign')
                self.assertNotIn('foreign NOW', now(details.merge(changed, detail).jobs[0]))

    def test_delayed_failed_and_then_successful_detail(self):
        import threading
        row = next(self.rows())
        source = LiveSnapshot(jobs=[row])
        release = threading.Event()
        def producer():
            release.wait(2)
            raise OSError('synthetic failure')
        pump = RefreshPump(producer, 20)
        pump.start()
        try:
            self.assertIn('loading', now(details.merge(source, None).jobs[0]))
            release.set()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not pump.health(time.monotonic()).get('last_error'):
                time.sleep(.005)
            self.assertIsNotNone(pump.health(time.monotonic())['last_error'])
            self.assertIsNone(pump.poll(0))
            self.assertIn('loading', now(details.merge(source, None).jobs[0]))
        finally:
            release.set()
            pump.stop()
        detail = details.DetailSnapshot(details.state_key(source), copy.deepcopy(source))
        detail.snapshot.jobs[0]._details_error = True
        self.assertIn('detail failed', now(details.merge(source, detail).jobs[0]))

    def test_native_detail_exception_is_visible_and_success_clears_it(self):
        from fleet.collectors import dispatch
        row = next(self.rows())
        source = LiveSnapshot(jobs=[row])
        with mock.patch.multiple(dispatch, _fill_locations=mock.DEFAULT,
                _campaign_labels=mock.DEFAULT, _enrich_claude_stream_session=mock.DEFAULT,
                _enrich_codex_attempt_session=mock.DEFAULT, _enrich_opencode_attempt_session=mock.DEFAULT,
                _enrich_attempt_summary=mock.DEFAULT, effective_qa=mock.DEFAULT,
                _scan_degradations=mock.DEFAULT, _pending_delivery_counts=mock.DEFAULT,
                _candidate_jobs_paths=mock.DEFAULT) as observers, \
                mock.patch('fleet.titles.annotate'), mock.patch('fleet.route_chain.enrich'), \
                mock.patch.object(projection, 'attach_projections'), \
                mock.patch('fleet.work_titles.annotate'), \
                mock.patch('fleet.collectors.peer_messages.collect', return_value=None), \
                mock.patch('fleet.herdr_projection.refresh_observed_tag_metadata'):
            observers['_enrich_claude_stream_session'].side_effect = OSError('fixture read failed')
            observers['_scan_degradations'].return_value = {}
            filled = details._enrich(source)
            merged = details.merge(source, filled)
            self.assertIn('detail failed', now(merged.jobs[0]))
            observers['_enrich_claude_stream_session'].side_effect = None
            observers['effective_qa'].return_value = ('quick', 'fixture')
            recovered = details.merge(merged, details._enrich(merged)).jobs[0]
            self.assertIn('no summary yet', now(recovered))
            self.assertNotIn('detail failed', now(recovered))


class NavigationTest(unittest.TestCase):
    def test_every_step_clamps_and_first_action_stays_queued(self):
        from fleet.tests.test_input_freeze import FakeScreen
        screen = FakeScreen([ord('j')]*140 + [ord('k')]*5 + [ord('x'), ord('y')])
        screen.getmaxyx = lambda: (36, 168)
        reader = render._CursesInput(curses)
        with mock.patch.multiple(render, _OFFSET=10, _CURRENT_BODY_COUNT=30,
                                 _CURRENT_BODY_H=20), mock.patch.object(render.time, 'monotonic', return_value=0):
            render._drain_navigation(reader, screen, ord('j'), None, 20, 37)
            self.assertEqual(render._OFFSET, 10)
            self.assertEqual(screen.delay, 37)
            ch, mouse = reader.read(screen)
            render._drain_navigation(reader, screen, ch, mouse, 20, 37)
            self.assertEqual(render._OFFSET, 5)
            self.assertEqual(reader.read(screen), (ord('x'), None))
            self.assertEqual(reader.read(screen), (ord('y'), None))

    def test_resize_fold_click_and_confirmation_keys_are_barriers(self):
        for ch, event in ((curses.KEY_RESIZE, None), (ord(' '), None),
                          (ord('x'), None), (ord('y'), None), (ord('Y'), None),
                          (curses.KEY_MOUSE, (1, 1, curses.BUTTON1_PRESSED))):
            self.assertFalse(render._base_navigation(ch, event))


def child(source_tree, sandbox, telemetry_fd):
    # Minimal environment is supplied by the parent before any Fleet import.
    sys.path.insert(0, str(Path(source_tree) / 'tools'))
    from fleet.tests import benchmark_navigation as bench
    fixture_path = Path(__file__).parent / 'fixtures/peer_navigation.json'
    fixture, _ = bench._load_fixture(fixture_path)
    bench._isolate_runtime(sandbox, fixture, 1000)
    fixture, _, r = bench._setup(source_tree, fixture_path)
    rows = bench._sessions(fixture, 100, protected=True)
    draw, base, mouse = r._draw, r._handle_base_key, r._handle_mouse_event
    count = [0, 0]
    def emit(value):
        bench._emit(telemetry_fd, value)
    def draw_record(screen, *args, **kwargs):
        result = draw(screen, *args, **kwargs)
        count[0] += 1
        if r._HIT_VALID:
            emit(dict(kind='draw', draws=count[0], handled=count[1], offset=r._OFFSET,
                maximum=max(0, r._CURRENT_BODY_COUNT-r._CURRENT_BODY_H),
                top=r._CURRENT_BODY_TOP, select=r._SELECT_MODE,
                prompt=(r._PROMPT or {}).get('stage'), ns=time.monotonic_ns()))
        return result
    def base_record(ch, body_h):
        result = base(ch, body_h)
        if result:
            count[1] += 1
            emit(dict(kind='step', key=ch, offset=r._OFFSET))
        return result
    def mouse_record(*args):
        result = mouse(*args)
        count[1] += 1
        emit(dict(kind='step', key='wheel', offset=r._OFFSET))
        return result
    r._draw, r._handle_base_key, r._handle_mouse_event = draw_record, base_record, mouse_record
    def collect_all(**_kwargs):
        return bench._live_snapshot(rows)
    curses.wrapper(r._loop, collect_all, None, 'both', 20)
    emit(dict(kind='exit', draws=count[0], handled=count[1], ns=time.monotonic_ns()))


def pty_probe(source_tree, sandbox, wheel=False, at_top=False, barrier='s'):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 36, 168, 0, 0))
    read_fd, write_fd = os.pipe()
    env = {'PATH': '/usr/bin:/bin', 'TERM': 'xterm-256color', 'LC_ALL': 'C.UTF-8',
           'HOME': str(sandbox), 'PYTHONDONTWRITEBYTECODE': '1'}
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--child',
        str(source_tree), str(sandbox), str(write_fd)], stdin=slave, stdout=slave,
        stderr=slave, env=env, cwd=sandbox, pass_fds=(write_fd,), start_new_session=True)
    os.close(slave)
    os.close(write_fd)
    os.set_blocking(master, False)
    os.set_blocking(read_fd, False)
    selector = selectors.DefaultSelector()
    selector.register(master, selectors.EVENT_READ)
    selector.register(read_fd, selectors.EVENT_READ)
    records, pending = [], bytearray()
    def drain(until, timeout=45):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for key, _ in selector.select(.02):
                try:
                    data = os.read(key.fd, 65536)
                except OSError:
                    data = b''
                if key.fd == read_fd:
                    pending.extend(data)
                    while b'\n' in pending:
                        line, _, tail = pending.partition(b'\n')
                        pending[:] = tail
                        records.append(json.loads(line))
            if until(records):
                return
            if process.poll() is not None:
                raise AssertionError('PTY exited early: %s' % process.returncode)
        raise AssertionError('PTY timeout')
    try:
        drain(lambda rs: any(x['kind'] == 'draw' for x in rs))
        os.write(master, b'g' if at_top else b'G')
        drain(lambda rs: any(x['kind'] == 'step' for x in rs))
        drain(lambda rs: rs[-1]['kind'] == 'draw')
        initial = records[-1]
        n = 240 if wheel else 600  # one wheel burst fits the kernel TTY input queue
        if wheel:
            y = initial['top'] + 1
            outward = ('\x1b[<%d;8;%dM' % (64 if at_top else 65, y)).encode()
            reverse = ('\x1b[<%d;8;%dM' % (65 if at_top else 64, y)).encode()
        else:
            outward, reverse = ((b'k', b'j') if at_top else (b'j', b'k'))
        start = time.monotonic_ns()
        os.write(master, outward*n + reverse*5 + barrier.encode())
        drain(lambda rs: any(x['kind']=='exit' if barrier=='q' else
              x['kind']=='draw' and (x.get('select') if barrier=='s' else x.get('prompt')) for x in rs))
        action = records[-1]
        navigation = [x for x in records if x['kind']=='draw' and not x['select']][-1]
        if barrier != 'q':
            os.write(master, b'q')
            drain(lambda rs: any(x['kind']=='exit' for x in rs))
        process.wait(timeout=3)
        return dict(source=str(source_tree), wheel=wheel, at_top=at_top,
            events=n+5, handled=action['handled']-initial['handled'],
            draws=action['draws']-initial['draws'], offset=navigation['offset'],
            expected_offset=5 if at_top else initial['maximum']-5,
            action_latency_ms=(action['ns']-start)/1e6,
            quit_latency_ms=(records[-1]['ns']-action['ns'])/1e6,
            prompt_stages=[x['prompt'] for x in records if x['kind']=='draw' and x.get('prompt')],
            steps=[x for x in records if x['kind']=='step'])
    finally:
        if process.poll() is None:
            process.kill()  # only this synthetic child
            process.wait()
        selector.close()
        os.close(master)
        os.close(read_fd)


class PtyTest(unittest.TestCase):
    def test_boundaries_reverse_and_action(self):
        for wheel in (False, True):
            for at_top in (False, True):
                with self.subTest(wheel=wheel, top=at_top), tempfile.TemporaryDirectory() as root:
                    result = pty_probe(TOOLS.parent, root, wheel, at_top)
                    self.assertEqual(result['offset'], result['expected_offset'])
                    self.assertEqual(result['handled'], result['events'])
                    self.assertLess(result['draws'], 30)

    def test_queued_confirmation_requires_both_distinct_keys(self):
        with tempfile.TemporaryDirectory() as root:
            result = pty_probe(TOOLS.parent, root, barrier='sxjjyyjn')
            # Kill is forbidden in the child. j and repeated lowercase y must
            # not move/approve past either confirmation, and n must cancel.
            self.assertGreaterEqual(result['prompt_stages'].count('confirm'), 3)
            self.assertGreaterEqual(result['prompt_stages'].count('confirm2'), 3)

    def test_quit_after_burst_is_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            result = pty_probe(TOOLS.parent, root, barrier='q')
            self.assertEqual(result['handled'], result['events'])
            self.assertEqual(result['offset'], result['expected_offset'])


if __name__ == '__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--child':
        child(sys.argv[2], sys.argv[3], int(sys.argv[4]))
    else:
        unittest.main()
