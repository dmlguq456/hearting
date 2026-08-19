"""F-27 — limited, user-initiated session control (PRD v8 §4.8, prd.md:250-255).

Hermetic by construction: no test here signals a real session. The only processes any test
may signal are disposable `sleep` fixtures it spawned itself; everything else uses injected
pids and a monkeypatched os.kill, and every action log goes to a temp FLEET_ACTION_STATE_DIR.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import control, render                                # noqa: E402
from fleet.model import DispatchJob, Session                     # noqa: E402


class ActionLogEnv(unittest.TestCase):
    """Base: every action log lands in a temp dir, never the user's real state dir."""

    def setUp(self):
        self.state = tempfile.mkdtemp()
        self._prev = os.environ.get("FLEET_ACTION_STATE_DIR")
        os.environ["FLEET_ACTION_STATE_DIR"] = self.state
        render.reset_selection()

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("FLEET_ACTION_STATE_DIR", None)
        else:
            os.environ["FLEET_ACTION_STATE_DIR"] = self._prev
        render.reset_selection()

    def _log_rows(self):
        path = os.path.join(self.state, "actions.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(l) for l in f if l.strip()]


class VerifyTargetTest(ActionLogEnv):
    """The PID-reuse guard — the single most important refusal in the module."""

    def test_exact_start_time_verifies(self):
        self.assertTrue(control.verify_target(os.getpid(),
                                              control.read_proc_start(os.getpid())))

    def test_mismatched_start_time_refuses(self):
        real = control.read_proc_start(os.getpid())
        self.assertFalse(control.verify_target(os.getpid(), str(int(real) + 1)))

    def test_absent_start_time_refuses(self):
        """No identity half → cannot prove the target → refuse (fail closed, not open)."""
        self.assertFalse(control.verify_target(os.getpid(), None))
        self.assertFalse(control.verify_target(os.getpid(), ""))

    def test_dead_pid_refuses(self):
        self.assertFalse(control.verify_target(999999, "12345"))

    def test_garbage_pid_refuses(self):
        self.assertFalse(control.verify_target("not-a-pid", "1"))
        self.assertFalse(control.verify_target(None, "1"))


class ExclusionTest(ActionLogEnv):
    """Hard guards — these pids are removed before a prompt can even be shown."""

    def test_fleet_itself_is_excluded(self):
        self.assertTrue(control.is_excluded(os.getpid()))

    def test_parent_is_excluded(self):
        self.assertTrue(control.is_excluded(os.getppid()))

    def test_whole_ancestry_is_excluded(self):
        """Killing any ancestor takes fleet (and the user's terminal) down with it."""
        anc = control._ancestors(os.getpid())
        self.assertTrue(anc, "no ancestors resolved — guard would be vacuous")
        for a in anc:
            self.assertTrue(control.is_excluded(a), "ancestor %d selectable" % a)

    def test_init_and_invalid_are_excluded(self):
        for bad in (1, 0, -5, None, "x"):
            self.assertTrue(control.is_excluded(bad), repr(bad))

    def test_current_session_pid_is_excluded(self):
        home = tempfile.mkdtemp()
        os.mkdir(os.path.join(home, "sessions"))
        with open(os.path.join(home, "sessions", "4242.json"), "w") as f:
            json.dump({"pid": 4242, "sessionId": "SID-X"}, f)
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "SID-X",
                                          "CLAUDE_CONFIG_DIR": home}):
            self.assertTrue(control.is_excluded(4242))

    def test_an_unrelated_pid_is_not_excluded(self):
        """The control: the guard must not just return True for everything."""
        p = subprocess.Popen(["sleep", "30"])
        try:
            self.assertFalse(control.is_excluded(p.pid))
        finally:
            p.kill()
            p.wait()


class KillRefusalTest(ActionLogEnv):
    """Refusals must be REAL: no signal leaves the process, and the reason is logged."""

    def setUp(self):
        super().setUp()
        self.killed = []

    def _spy_kill(self, pid, sig):
        self.killed.append((pid, sig))

    def test_real_start_time_mismatch_sends_no_signal(self):
        """A LIVE pid whose start-time differs = the pid was recycled. This is the case the
        guard exists for, and the only one that may be logged as start_time_mismatch."""
        p = subprocess.Popen(["sleep", "30"])
        try:
            forged = str(int(control.read_proc_start(p.pid)) + 1)
            with mock.patch.object(os, "kill", self._spy_kill):
                r = control.kill_target(p.pid, forged, None, "unused", "single")
            self.assertEqual(r, "refused")
            self.assertEqual(self.killed, [], "★ 거부됐는데 시그널이 나갔다")
            rows = self._log_rows()
            self.assertEqual(rows[-1]["result"], "refused")
            self.assertEqual(rows[-1]["reason"], "start_time_mismatch")
        finally:
            p.kill()
            p.wait()

    def test_refusal_reasons_are_distinct_and_truthful(self):
        """phase_02 review C1-3: `no_proc_start` (never collected), `target_gone` (pid
        vanished) and `start_time_mismatch` (pid recycled) are three different facts. The
        action log is the audit trail — reporting the first two as the third would claim
        fleet detected PID reuse when it detected no such thing."""
        p = subprocess.Popen(["sleep", "30"])
        try:
            real = control.read_proc_start(p.pid)
            self.assertEqual(control.verify_reason(p.pid, None), "no_proc_start")
            self.assertEqual(control.verify_reason(999999, "1"), "target_gone")
            self.assertEqual(control.verify_reason(p.pid, str(int(real) + 1)),
                             "start_time_mismatch")
            self.assertIsNone(control.verify_reason(p.pid, real))
        finally:
            p.kill()
            p.wait()

    def test_missing_proc_start_is_logged_as_such_not_as_reuse(self):
        with mock.patch.object(os, "kill", self._spy_kill):
            r = control.kill_target(999999, None, None, "unused", "single")
        self.assertEqual(r, "refused")
        self.assertEqual(self.killed, [])
        self.assertEqual(self._log_rows()[-1]["reason"], "no_proc_start")

    def test_excluded_target_sends_no_signal(self):
        own = os.getpid()
        with mock.patch.object(os, "kill", self._spy_kill):
            r = control.kill_target(own, control.read_proc_start(own), None, "unused", "single")
        self.assertEqual(r, "refused")
        self.assertEqual(self.killed, [])
        self.assertEqual(self._log_rows()[-1]["reason"], "excluded_target")

    def test_missing_approval_sends_no_signal(self):
        with mock.patch.object(os, "kill", self._spy_kill):
            r = control.kill_target(999999, "1", None, "unused", None)
        self.assertEqual(r, "refused")
        self.assertEqual(self.killed, [])
        self.assertEqual(self._log_rows()[-1]["reason"], "no_approval")

    def test_live_session_refuses_single_confirmation(self):
        """A `working` row may not be terminated on one keystroke (prd.md:253)."""
        p = subprocess.Popen(["sleep", "30"])
        try:
            with mock.patch.object(os, "kill", self._spy_kill):
                r = control.kill_target(p.pid, control.read_proc_start(p.pid), None,
                                        "working", "single")
            self.assertEqual(r, "refused")
            self.assertEqual(self.killed, [])
            self.assertEqual(self._log_rows()[-1]["reason"], "live_target_needs_double_confirm")
        finally:
            p.kill()
            p.wait()

    def test_double_confirmation_allows_a_live_target(self):
        p = subprocess.Popen(["sleep", "30"])
        try:
            with mock.patch.object(os, "kill", self._spy_kill):
                r = control.kill_target(p.pid, control.read_proc_start(p.pid), None,
                                        "working", "double")
            self.assertEqual(r, "ok")
            self.assertEqual(self.killed, [(p.pid, signal.SIGTERM)])
        finally:
            p.kill()
            p.wait()


class RealSignalTest(ActionLogEnv):
    """The one place a signal is really sent — always to a `sleep` we spawned ourselves."""

    def test_verified_target_gets_sigterm_and_dies(self):
        p = subprocess.Popen(["sleep", "300"])
        time.sleep(0.2)
        start = control.read_proc_start(p.pid)
        r = control.kill_target(p.pid, start, None, "unused", "single")
        self.assertEqual(r, "ok")
        p.wait(timeout=5)
        self.assertEqual(p.returncode, -signal.SIGTERM)
        self.assertEqual(self._log_rows()[-1]["action"], "sigterm")

    def test_forged_start_time_leaves_the_process_alive(self):
        """The acceptance in full: refuse, and the target actually survives."""
        p = subprocess.Popen(["sleep", "300"])
        time.sleep(0.2)
        start = control.read_proc_start(p.pid)
        try:
            r = control.kill_target(p.pid, str(int(start) + 1), None, "unused", "single")
            self.assertEqual(r, "refused")
            time.sleep(0.2)
            self.assertIsNone(p.poll(), "★ 거부됐어야 할 대상이 실제로 죽었다")
        finally:
            p.kill()
            p.wait()

    def test_escalation_sends_sigkill_only_when_asked(self):
        p = subprocess.Popen(["sleep", "300"])
        time.sleep(0.2)
        start = control.read_proc_start(p.pid)
        r = control.kill_target(p.pid, start, None, "unused", "escalated")
        self.assertEqual(r, "ok")
        p.wait(timeout=5)
        self.assertEqual(p.returncode, -signal.SIGKILL)
        self.assertEqual(self._log_rows()[-1]["action"], "sigkill")


class ActionLogTest(ActionLogEnv):
    def test_rotation_is_bounded_to_two_files(self):
        path = os.path.join(self.state, "actions.jsonl")
        os.makedirs(self.state, exist_ok=True)
        with open(path, "w") as f:
            f.write("x" * (control.ACTION_LOG_MAX_BYTES + 1))
        control.log_action(action="refused", pid=1, result="refused", reason="t")
        self.assertTrue(os.path.exists(path + ".1"))
        self.assertLess(os.path.getsize(path), control.ACTION_LOG_MAX_BYTES)
        # A second rotation overwrites .1 — never a third file.
        with open(path, "w") as f:
            f.write("y" * (control.ACTION_LOG_MAX_BYTES + 1))
        control.log_action(action="refused", pid=2, result="refused", reason="t")
        self.assertFalse(os.path.exists(path + ".2"))

    def test_log_failure_never_raises(self):
        """Losing the audit line must not change what happens to a signal, or crash the TUI."""
        with mock.patch.dict(os.environ, {"FLEET_ACTION_STATE_DIR": "/proc/nonexistent/x"}):
            control.log_action(action="sigterm", pid=1, result="ok")   # must not raise

    def test_row_carries_every_audit_field(self):
        control.log_action(action="sigterm", pid=7, sid="s", state="unused",
                           approval="single", result="ok", reason=None)
        row = self._log_rows()[-1]
        for k in ("ts", "action", "pid", "sid", "state", "approval", "result", "reason"):
            self.assertIn(k, row)


# --- registry close: exact-attempt typed cancellation -------------------------------

def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


CURRENT = (
    "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
    "execution_surface=registered-headless,registered_worker=1,"
    "fallback_hop=same-harness-headless,"
)

ROWS = (
    "2026-07-15T10:00:00\topen\t/repo\t/wt/a\tslug-a\t"
    + CURRENT + "attempt_id=att-fleet-a,capability=code,mode=dev\n"
    "2026-07-15T10:01:00\trunning\t/repo\t/wt/b\tslug-b\t"
    + CURRENT + "attempt_id=att-fleet-b,capability=code\n"
    "2026-07-15T10:02:00\topen\t/repo\t/wt/c\tslug-c\t"
    + CURRENT + "attempt_id=att-fleet-c,capability=spec\n"
)


class RegistryCloseTest(ActionLogEnv):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp()
        self.jobs = os.path.join(self.dir, "jobs.log")
        with open(self.jobs, "w") as f:
            f.write(ROWS)

    def test_closes_the_matching_open_row(self):
        self.assertTrue(control.close_registry_row(self.jobs, "att-fleet-a"))
        out = _read(self.jobs)
        self.assertIn("done\t/repo\t/wt/a\tslug-a\t", out)
        self.assertIn("note=fleet-kill", out)
        self.assertIn("failure_class=cancelled", out)
        self.assertIn("classifier_source=fleet-cancel-v1", out)

    def test_spec_note_token_is_fleet_kill(self):
        """prd.md:255 names this token; it is what distinguishes an external console kill
        from a dispatch that died on its own (`note=dead-<reason>`)."""
        control.close_registry_row(self.jobs, "att-fleet-a")
        out = _read(self.jobs)
        self.assertIn("note=fleet-kill", out)
        self.assertNotIn("note=dead-", out)

    def test_no_match_is_idempotent_and_touches_nothing(self):
        before = _read(self.jobs)
        self.assertFalse(control.close_registry_row(self.jobs, "att-missing"))
        self.assertEqual(_read(self.jobs), before)

    def test_same_slug_sibling_is_untouched(self):
        with open(self.jobs, "a") as f:
            f.write(
                "2026-07-15T10:03:00\topen\t/repo\t/wt/other\tslug-a\t"
                + CURRENT + "attempt_id=att-fleet-sibling,capability=code\n"
            )
        self.assertTrue(control.close_registry_row(self.jobs, "att-fleet-a"))
        out = _read(self.jobs)
        sibling = next(line for line in out.splitlines() if "att-fleet-sibling" in line)
        self.assertIn("\topen\t", sibling)
        self.assertNotIn("fleet-kill", sibling)

    def test_a_running_row_is_typed_cancelled(self):
        self.assertTrue(control.close_registry_row(self.jobs, "att-fleet-b"))
        line = next(line for line in _read(self.jobs).splitlines() if "att-fleet-b" in line)
        self.assertIn("\tdone\t", line)
        self.assertIn("failure_class=cancelled", line)

    def test_missing_file_is_false_not_a_crash(self):
        self.assertFalse(control.close_registry_row(
            os.path.join(self.dir, "nope.log"), "att-fleet-a"
        ))

    def test_legacy_row_is_diagnostic_only(self):
        legacy = (
            "2026-07-15T10:00:00\topen\t/repo\t/wt/a\tslug-a\t"
            "capability=code\n"
        )
        with open(self.jobs, "w") as f:
            f.write(legacy)
        self.assertFalse(control.close_registry_row(self.jobs, "att-fleet-a"))
        self.assertEqual(_read(self.jobs), legacy)

    def test_duplicate_attempt_identity_fails_closed(self):
        with open(self.jobs, "w") as f:
            f.write((
                "2026-07-15T10:00:00\topen\t/repo\t/wt/a\tslug-a\t"
                + CURRENT + "attempt_id=att-duplicate,p=1\n"
            ) * 3)
        self.assertFalse(control.close_registry_row(self.jobs, "att-duplicate"))
        out = _read(self.jobs)
        self.assertEqual(out.count("\tdone\t"), 0)
        self.assertEqual(out.count("\topen\t"), 3)

    def test_close_registry_row_holds_the_canonical_flock(self):
        import multiprocessing
        p = self.jobs
        lock_path = p + ".lock"
        held = multiprocessing.Event()
        release = multiprocessing.Event()
        done = multiprocessing.Event()

        def holder():
            import fcntl as f2
            with open(lock_path, "a") as fh:
                f2.flock(fh.fileno(), f2.LOCK_EX)
                held.set()
                release.wait(10)
                f2.flock(fh.fileno(), f2.LOCK_UN)

        def closer():
            control.close_registry_row(p, "att-fleet-a")
            done.set()

        h = multiprocessing.Process(target=holder)
        h.start()
        self.assertTrue(held.wait(5), "lock holder did not start")
        c = multiprocessing.Process(target=closer)
        c.start()
        try:
            # While the lock is held, the close must NOT complete.
            self.assertFalse(done.wait(1.0),
                             "★ close_registry_row 이 flock 을 기다리지 않는다 — 직렬화 없음")
            release.set()
            self.assertTrue(done.wait(5), "close never completed after the lock was released")
            self.assertIn("note=fleet-kill", _read(p))
        finally:
            release.set()
            for proc in (c, h):
                proc.join(5)
                if proc.is_alive():
                    proc.terminate()


class NoAutomaticControlTest(unittest.TestCase):
    """prd.md:253 — zero automatic control. The static half of the guard; the measured half
    (no action log after --json/--once) lives in the step's verification commands."""

    def test_only_render_imports_control(self):
        """A collector or snapshot path that could import control could, one refactor later,
        call it. The boundary is that they do not know it exists."""
        import subprocess as sp
        root = os.path.join(os.path.dirname(__file__), "..")
        out = sp.run(["grep", "-rn", "-E", r"import control|from \. import control",
                      root, "--include=*.py"], capture_output=True, text=True).stdout
        offenders = [l for l in out.splitlines()
                     if "/tests/" not in l and "/render.py" not in l and "/control.py" not in l]
        self.assertEqual(offenders, [], "control imported outside render.py: %s" % offenders)

    def test_control_is_not_reachable_from_collect_all(self):
        import fleet.collectors as collectors
        src = open(collectors.__file__).read()
        self.assertNotIn("control", src)


class SelectionModeTest(ActionLogEnv):
    """The moded cursor (plan §6.2). Tested without curses — that is why the mode logic
    lives in helpers rather than inline in _loop."""

    def _rows(self):
        ghost = Session(harness="claude", pid=90001, cwd="/home/u/p", slug="agent-setting-17",
                        liveness="unused", elapsed_min=225)
        ghost.registry_name = "agent-setting-17"
        ghost.proc_start = "111"
        live = Session(harness="claude", pid=90002, cwd="/home/u/p", slug="p",
                       liveness="working", title="Real work")
        live.proc_start = "222"
        idle = Session(harness="claude", pid=90003, cwd="/home/u/p", slug="q",
                       liveness="idle", title="Someone's idle session")
        idle.proc_start = "333"
        return [ghost, live, idle]

    def _build(self):
        render._build_lines(self._rows(), [], section="fleet", narrow=False,
                            malformed=0, term_width=168)

    def test_unused_and_working_are_selectable_but_plain_idle_is_not(self):
        self._build()
        pids = [e["pid"] for e in render._SELECTABLE]
        self.assertIn(90001, pids)          # unused → grade 1
        self.assertIn(90002, pids)          # working → grade 2 (double confirm)
        self.assertNotIn(90003, pids)       # a live idle session is nobody's cleanup target

    def test_selectable_is_reset_every_build(self):
        self._build()
        n = len(render._SELECTABLE)
        self._build()
        self.assertEqual(len(render._SELECTABLE), n, "stale targets accumulated")

    def test_build_lines_return_contract_is_unchanged(self):
        """The stash is additive — render_once must be untouched (R11)."""
        lines = render._build_lines(self._rows(), [], section="fleet", narrow=False,
                                    malformed=0, term_width=168)
        self.assertIsInstance(lines, list)
        self.assertTrue(any(ln is None or isinstance(ln, list) for ln in lines))

    def test_excluded_pids_never_become_targets(self):
        s = Session(harness="claude", pid=os.getpid(), cwd="/home/u/p", slug="self",
                    liveness="unused", elapsed_min=1)
        render._build_lines([s], [], section="fleet", narrow=False, malformed=0,
                            term_width=168)
        self.assertIn(os.getpid(), [e["pid"] for e in render._SELECTABLE])
        self.assertEqual([e["pid"] for e in render._live_targets()], [],
                         "★ fleet 자신이 선택 가능한 타겟")

    def test_x_from_base_mode_selects_and_does_not_kill(self):
        self._build()
        targets = render._live_targets()
        self.assertTrue(render._enter_select(targets))
        self.assertTrue(render._SELECT_MODE)
        self.assertIsNone(render._PROMPT)          # entering never prompts

    def test_x_in_select_mode_raises_a_prompt_but_sends_no_signal(self):
        self._build()
        render._enter_select(render._live_targets())
        with mock.patch.object(os, "kill") as k:
            render._handle_select_key(ord("x"))
            self.assertIsNotNone(render._PROMPT)
            self.assertEqual(render._PROMPT["stage"], "confirm")
            k.assert_not_called()                  # a prompt is not a kill

    def test_escape_exits_selection(self):
        self._build()
        render._enter_select(render._live_targets())
        render._handle_select_key(render._ESC)
        self.assertFalse(render._SELECT_MODE)

    def test_cursor_moves_and_clamps(self):
        self._build()
        targets = render._live_targets()
        render._enter_select(targets)
        render._handle_select_key(curses_up())
        self.assertEqual(render._cursor_index(targets), 0)              # clamps at the top
        for _ in range(10):
            render._handle_select_key(curses_down())
        self.assertEqual(render._cursor_index(targets), len(targets) - 1)   # clamps at bottom

    def test_cursor_holds_its_target_across_a_rebuild(self):
        """phase_02 review M3: an index-based cursor silently re-aims when the board rebuilds
        (rows come and go every tick), so the user aims at A and the prompt names B. Anchoring
        on (pid, proc_start) means a rebuild finds the same row or none."""
        self._build()
        render._enter_select(render._live_targets())
        # Aim explicitly at 90002 (row order is the renderer's business, not this test's).
        render._CURSOR_ID = next(render._entry_id(e) for e in render._live_targets()
                                 if e["pid"] == 90002)
        aimed = render._live_targets()[render._cursor_index(render._live_targets())]
        self.assertEqual(aimed["pid"], 90002)

        # Rebuild with the ghost row gone — an index cursor would now point at a different row.
        ghost_gone = [s for s in self._rows() if s.pid != 90001]
        render._build_lines(ghost_gone, [], section="fleet", narrow=False, malformed=0,
                            term_width=168)
        targets = render._live_targets()
        still = targets[render._cursor_index(targets)]
        self.assertEqual(still["pid"], 90002, "커서가 리빌드 중 다른 행으로 재조준됨")

    def test_cursor_drops_when_its_target_disappears(self):
        """If the aimed row is gone, re-anchor at the top — and swallow that `x` so a kill
        request can never land on a row the user never chose."""
        self._build()
        render._enter_select(render._live_targets())
        render._CURSOR_ID = (999999, "gone")        # aim at something no longer on the board
        with mock.patch.object(os, "kill") as k:
            render._handle_select_key(ord("x"))
            k.assert_not_called()
        self.assertIsNone(render._PROMPT, "사라진 행에 kill 프롬프트가 떴다")
        self.assertEqual(render._CURSOR_ID, _first_target_id())


def _first_target_id():
    t = render._live_targets()
    return render._entry_id(t[0])


def curses_up():
    import curses
    return curses.KEY_UP


def curses_down():
    import curses
    return curses.KEY_DOWN


class ConfirmationFlowTest(ActionLogEnv):
    """No signal without an explicit human yes — and a live target needs two."""

    def _entry(self, state="unused", status=None):
        return {"line": 0, "kind": "session", "pid": 90001, "proc_start": "111",
                "sid": None, "state": state, "status": status, "cwd": "/x",
                "slug": "s", "label": "agent-setting-17", "harness": "claude",
                "source": None}

    def test_a_random_key_is_not_consent(self):
        render._PROMPT = {"stage": "confirm", "entry": self._entry()}
        with mock.patch.object(render, "_do_kill") as d:
            for ch in (ord("a"), ord("z"), ord("Y"), ord(" "), 10):
                render._handle_prompt_key(ch)
                d.assert_not_called()
        self.assertIsNotNone(render._PROMPT)     # still asking

    def test_timeout_tick_does_not_answer_the_prompt(self):
        """The loop wakes ~10x/sec with getch()==-1; that must never count as a decision."""
        render._PROMPT = {"stage": "confirm", "entry": self._entry()}
        with mock.patch.object(render, "_do_kill") as d:
            render._handle_prompt_key(-1)
            d.assert_not_called()
        self.assertIsNotNone(render._PROMPT)

    def test_escape_cancels(self):
        render._PROMPT = {"stage": "confirm", "entry": self._entry()}
        with mock.patch.object(render, "_do_kill") as d:
            render._handle_prompt_key(render._ESC)
            d.assert_not_called()
        self.assertIsNone(render._PROMPT)

    def test_y_kills_a_single_confirm_target(self):
        render._PROMPT = {"stage": "confirm", "entry": self._entry("unused")}
        with mock.patch.object(render, "_do_kill") as d:
            render._handle_prompt_key(ord("y"))
            d.assert_called_once()
            self.assertEqual(d.call_args[0][1], "single")

    def test_live_target_needs_a_second_different_key(self):
        render._PROMPT = {"stage": "confirm", "entry": self._entry("working")}
        with mock.patch.object(render, "_do_kill") as d:
            render._handle_prompt_key(ord("y"))
            d.assert_not_called()                       # first y only escalates the prompt
            self.assertEqual(render._PROMPT["stage"], "confirm2")
            render._handle_prompt_key(ord("y"))         # holding `y` must NOT walk through
            d.assert_not_called()
            render._handle_prompt_key(ord("Y"))         # a DIFFERENT key is required
            d.assert_called_once()
            self.assertEqual(d.call_args[0][1], "double")

    def test_registry_busy_also_needs_double_confirm(self):
        self.assertTrue(control.requires_double_confirm("idle", "busy"))
        self.assertTrue(control.requires_double_confirm("working", None))
        self.assertFalse(control.requires_double_confirm("unused", "idle"))


class SingleConfirmWhitelistTest(ActionLogEnv):
    """prd.md:251's allowed-target list, enforced inside control (phase_02 review M2).

    It must be a WHITELIST: control.py claims to be fail-closed and to be the testable half
    of the safety contract, so an unknown state must not sail through on one keystroke just
    because nobody blacklisted it.
    """

    def test_basic_allowed_session_states(self):
        for st in ("unused", "stale", "dead"):
            self.assertTrue(control.single_confirm_allowed(st), st)

    def test_idle_worker_is_allowed_but_a_plain_idle_session_is_not(self):
        self.assertTrue(control.single_confirm_allowed("idle", is_worker=True))
        self.assertFalse(control.single_confirm_allowed("idle", is_worker=False))

    def test_live_session_is_not_single_confirmable(self):
        self.assertFalse(control.single_confirm_allowed("working"))
        self.assertFalse(control.single_confirm_allowed("idle", registry_status="busy"))

    def test_registry_job_is_basic_allowed_regardless_of_state(self):
        """prd.md:251 scopes the double-confirm to SESSIONS; a registry job with an exact pid
        is listed as basic-allowed with no state qualifier."""
        for st in ("working", "queued", "stale", "dead"):
            self.assertTrue(control.single_confirm_allowed(st, kind="job"), st)

    def test_unknown_state_defaults_to_deny(self):
        """The point of a whitelist: a state nobody thought about must NOT pass."""
        for st in ("blocked", "compacting", "", None, "brand-new-state"):
            self.assertFalse(control.single_confirm_allowed(st), repr(st))

    def test_kill_target_enforces_the_whitelist_itself(self):
        """Not just the UI — control must refuse on its own, since that is why it exists."""
        p = subprocess.Popen(["sleep", "30"])
        try:
            start = control.read_proc_start(p.pid)
            with mock.patch.object(os, "kill") as k:
                r = control.kill_target(p.pid, start, None, "idle", "single")   # plain idle
                self.assertEqual(r, "refused")
                k.assert_not_called()
            self.assertEqual(self._log_rows()[-1]["reason"],
                             "live_target_needs_double_confirm")
        finally:
            p.kill()
            p.wait()

    def test_kill_target_honors_the_registry_status_axis(self):
        """The caller passes status; control must not silently drop that axis."""
        p = subprocess.Popen(["sleep", "30"])
        try:
            start = control.read_proc_start(p.pid)
            with mock.patch.object(os, "kill") as k:
                r = control.kill_target(p.pid, start, None, "idle", "single",
                                        registry_status="busy", is_worker=True)
                self.assertEqual(r, "refused", "busy 축이 무시됨")
                k.assert_not_called()
        finally:
            p.kill()
            p.wait()

    def test_job_row_kill_actually_works_end_to_end(self):
        """phase_02 review C1: DispatchJob had no proc_start, so EVERY job kill was refused
        and prd.md:255's registry-close exception was unreachable dead code."""
        p = subprocess.Popen(["sleep", "30"])
        try:
            start = control.read_proc_start(p.pid)
            self.assertTrue(start, "job identity half missing")
            r = control.kill_target(p.pid, start, None, "working", "single",
                                    registry_status="open", kind="job")
            self.assertEqual(r, "ok")
            p.wait(timeout=5)
            self.assertEqual(p.returncode, -signal.SIGTERM)
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()


class DispatchJobIdentityTest(unittest.TestCase):
    """C1 root cause: a job row needs BOTH halves of the pid's identity to be killable."""

    def test_dispatchjob_carries_proc_start(self):
        self.assertIn("proc_start", DispatchJob(key="code").to_dict())

    def test_proc_start_defaults_to_none(self):
        self.assertIsNone(DispatchJob(key="code").proc_start)

    def test_reconcile_absorbs_proc_start_with_the_pid(self):
        """The pid and its start-time must always travel together — absorbing one without the
        other yields a row that looks killable and never is."""
        from fleet.collectors import dispatch
        from fleet import model
        model.reset_state_tracker()
        registry = DispatchJob(key="drill", slug="drill-claude-CASE-20260711160000-12345",
                               cwd="/tmp/drill-CASE-x/repo", pid=None, source="jobs",
                               status="open", elapsed_min=5)
        proc = DispatchJob(key="drill", slug="drill", worker_role="CASE",
                           cwd="/tmp/drill-CASE-x/repo", pid=999, source="proc",
                           proc_start="777777")
        jobs = dispatch._reconcile_drill_rows([registry, proc], now=1000.0)
        self.assertEqual(jobs[0].pid, 999)
        self.assertEqual(jobs[0].proc_start, "777777")


class PromptAffordanceTest(ActionLogEnv):
    """A confirmation the user cannot fully read is not a confirmation.

    The footer is clipped at the terminal edge, and the keys live at the END of the phrase —
    so on a narrow terminal the full wording would ask for consent while hiding how to give
    (or refuse) it. Every prompt must fit, at every supported width, with both the target and
    the keys intact.
    """

    E_UNUSED = {"line": 0, "kind": "session", "pid": 1168514, "proc_start": "3918896",
                "sid": "s", "state": "unused", "status": "idle", "cwd": "/x", "slug": "a",
                "label": "agent-setting-17", "harness": "claude", "source": None}
    E_WORKING = dict(E_UNUSED, state="working", status="busy", pid=2119125,
                     label="Fix breadcrumb cutoff in wide layout")

    def _stages(self):
        return (("confirm", self.E_UNUSED), ("confirm", self.E_WORKING),
                ("confirm2", self.E_WORKING), ("escalate", self.E_UNUSED))

    def test_every_prompt_fits_at_every_width(self):
        for w in (60, 80, 120, 168):
            for stage, entry in self._stages():
                segs = render._prompt_segs({"stage": stage, "entry": entry}, w)
                got = sum(render._dw(t) for t, _k in segs)
                self.assertLessEqual(got, w, "%s prompt is %d cells at width %d"
                                     % (stage, got, w))

    def test_keys_survive_the_compact_form(self):
        for stage, entry in self._stages():
            txt = "".join(t for t, _k in render._prompt_segs({"stage": stage,
                                                              "entry": entry}, 60))
            self.assertIn("Esc", txt, "%s: no way to refuse" % stage)
            self.assertTrue("y" in txt or "Y" in txt, "%s: no way to consent" % stage)

    def test_target_is_identified_in_the_compact_form(self):
        """The name may be dropped; the pid may not — it is unambiguous."""
        for stage, entry in self._stages():
            txt = "".join(t for t, _k in render._prompt_segs({"stage": stage,
                                                              "entry": entry}, 60))
            self.assertIn(str(entry["pid"]), txt, "%s: target unidentified" % stage)

    def test_live_target_keeps_its_warning_when_compact(self):
        for stage in ("confirm", "confirm2"):
            segs = render._prompt_segs({"stage": stage, "entry": self.E_WORKING}, 60)
            self.assertTrue(any(k in ("hdr_warn", "hdr_warn_key") for _t, k in segs),
                            "%s: warning styling lost when compact" % stage)

    def test_warning_prompts_are_still_full_width_bars(self):
        """design_critic_step4 CRITICAL: `_addline` decides bar-vs-fragment from the FIRST
        segment's role, so a warning head of `g_dead` made exactly the two prompts that must
        look MOST serious lose their band and render as a red-text/black-tail fragment — while
        the benign prompt got a clean bar. The hierarchy was inverted. The warning bar must be
        a bar."""
        for stage, entry in (("confirm", self.E_WORKING), ("confirm2", self.E_WORKING),
                             ("escalate", self.E_UNUSED)):
            for w in (60, 168):
                segs = render._prompt_segs({"stage": stage, "entry": entry}, w)
                self.assertIn(segs[0][1], render._BAR_ROLES,
                              "%s@%d leads with %r → _addline paints no band"
                              % (stage, w, segs[0][1]))
                self.assertEqual(segs[0][1], "hdr_warn",
                                 "%s@%d should lead with the RED bar" % (stage, w))

    def test_benign_prompt_uses_the_normal_bar(self):
        """Monotonic severity: benign = white/cyan bar, live+SIGKILL = red bar."""
        for w in (60, 168):
            segs = render._prompt_segs({"stage": "confirm", "entry": self.E_UNUSED}, w)
            self.assertEqual(segs[0][1], "hdr_bar")

    def test_benign_prompt_keeps_the_name_at_60(self):
        """design_critic_step4 §2: the full form overshot 60 by ~3 cells and the code fell
        straight to pid-only, discarding the NAME while leaving ~27 cells unused. A middle
        rung that trims decoration keeps the identity."""
        txt = "".join(t for t, _k in render._prompt_segs(
            {"stage": "confirm", "entry": self.E_UNUSED}, 60))
        self.assertIn("agent-setting-17", txt)

    def test_escalate_requires_the_capital_key_too(self):
        """SIGKILL is the most destructive act here; it must not ride the same keystroke that
        started the SIGTERM (the confirm2 rationale, applied consistently)."""
        txt = "".join(t for t, _k in render._prompt_segs(
            {"stage": "escalate", "entry": self.E_UNUSED}, 168))
        self.assertIn("Y", txt)
        self.assertIn("SIGKILL", txt)

    def test_compact_confirm2_still_demands_the_capital_key(self):
        txt = "".join(t for t, _k in render._prompt_segs(
            {"stage": "confirm2", "entry": self.E_WORKING}, 60))
        self.assertIn("Y", txt)

    def test_full_phrasing_is_kept_when_it_fits(self):
        txt = "".join(t for t, _k in render._prompt_segs(
            {"stage": "confirm2", "entry": self.E_WORKING}, 168))
        self.assertIn("capital", txt)

    def test_select_footer_fits_at_60(self):
        segs = render._footer_segs(True, ["↓12"])
        w = sum(render._dw(t) for t, _k in segs if t != render._RFLUSH)
        self.assertLessEqual(w, 60)


class GraceEscalationTest(ActionLogEnv):
    """SIGTERM → grace → ASK AGAIN. Never SIGTERM → grace → SIGKILL."""

    def _entry(self, pid, start):
        return {"line": 0, "kind": "session", "pid": pid, "proc_start": start,
                "sid": None, "state": "unused", "status": None, "cwd": "/x",
                "slug": "s", "label": "fixture", "harness": "claude", "source": None}

    def test_grace_expiry_prompts_instead_of_escalating(self):
        p = subprocess.Popen(["sleep", "300"])
        time.sleep(0.2)
        try:
            entry = self._entry(p.pid, control.read_proc_start(p.pid))
            render._PENDING_KILL = {"entry": entry,
                                    "since": time.time() - control.KILL_GRACE_SEC - 1}
            with mock.patch.object(os, "kill") as k:
                render._poll_pending_kill()
                k.assert_not_called()            # ★ no automatic SIGKILL
            self.assertIsNotNone(render._PROMPT)
            self.assertEqual(render._PROMPT["stage"], "escalate")
            self.assertIsNone(p.poll())          # and the process is still alive
        finally:
            p.kill()
            p.wait()

    def test_no_prompt_before_the_grace_expires(self):
        render._PENDING_KILL = {"entry": self._entry(os.getpid(),
                                                     control.read_proc_start(os.getpid())),
                                "since": time.time()}
        render._poll_pending_kill()
        self.assertIsNone(render._PROMPT)

    def test_target_that_died_clears_without_prompting(self):
        render._PENDING_KILL = {"entry": self._entry(999999, "1"),
                                "since": time.time() - control.KILL_GRACE_SEC - 1}
        render._poll_pending_kill()
        self.assertIsNone(render._PROMPT)
        self.assertIsNone(render._PENDING_KILL)

    def test_declining_escalation_stops_the_asking(self):
        render._PENDING_KILL = {"entry": self._entry(90001, "111"),
                                "since": time.time()}
        render._PROMPT = {"stage": "escalate", "entry": self._entry(90001, "111")}
        render._handle_prompt_key(render._ESC)
        self.assertIsNone(render._PROMPT)
        self.assertIsNone(render._PENDING_KILL)


if __name__ == "__main__":
    unittest.main()
