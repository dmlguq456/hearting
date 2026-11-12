"""F-47 — exec visibility: "running something and waiting" vs idle (PRD v30 §4.8/§7).

Every case is HERMETIC. The process table is a literal `ps` fixture (`proc_tree` is the one
seam), evidence dicts are hand-built, and no test reads /proc, ~/.claude, or the clock.

Coverage maps 1:1 onto the v30 contract:
  (a) shell + long-lived child      → working + `⚙` detail        (prd.md:612)
  (b) shell + no child              → idle, unchanged             (prd.md:612)
  (c) idle  + child (background)    → idle held, badge data only  (prd.md:263)
  (d) shell wrapper descent zsh→python                            (prd.md:263)
  (e) child younger than 60s        → no evidence at all          (prd.md:263)
  (f) codex unclosed tool_call detail, `task_started` rule intact (prd.md:263)
  (g) group tier / pulse census follow classification only        (prd.md:263)
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import model, render                                  # noqa: E402
from fleet.collectors import codex, liveness, procscan           # noqa: E402
from fleet.model import Session                                  # noqa: E402


def ps_tree(rows):
    """rows = [(pid, ppid, etimes, comm)] → the exact stdout `procscan.proc_tree` parses."""
    return "".join("%d %d %d %s\n" % row for row in rows)


def tree_of(rows):
    with mock.patch.object(procscan.subprocess, "run",
                           return_value=mock.Mock(stdout=ps_tree(rows))):
        tree = procscan.proc_tree()
    return tree, procscan.children_index(tree)


def no_helper_env():
    """Every candidate child reads as a real workload (env markers absent)."""
    return mock.patch.object(procscan, "read_environ", return_value={})


class ExecChildScan(unittest.TestCase):
    """collectors/procscan.py — the owned process-subtree evidence."""

    def test_direct_long_lived_child(self):
        tree, kids = tree_of([(100, 1, 3600, "claude"), (200, 100, 720, "python3")])
        with no_helper_env():
            found = procscan.exec_child(100, tree, kids)
        self.assertEqual(found, {"pid": 200, "comm": "python3", "etime_s": 720,
                                 "leaf_etime_s": 720, "kind": "work", "child_pid": 200})

    def test_no_child_is_no_evidence(self):
        tree, kids = tree_of([(100, 1, 3600, "claude")])
        with no_helper_env():
            self.assertIsNone(procscan.exec_child(100, tree, kids))

    def test_wrapper_descends_to_real_work(self):
        # Claude's Bash tool always runs `zsh -c '<snapshot> && <command>'`, so the direct
        # child names the wrapper and the interesting comm is one level down.
        tree, kids = tree_of([(100, 1, 3600, "claude"),
                              (200, 100, 720, "zsh"),
                              (300, 200, 700, "python3")])
        with no_helper_env():
            found = procscan.exec_child(100, tree, kids)
        self.assertEqual(found["comm"], "python3")
        self.assertEqual(found["pid"], 300)
        # 2026-09-16 — elapsed is the CALL's (the direct child / wrapper), because that is how long
        # the session has been on this one Bash call; the leaf's own age is kept beside it.
        self.assertEqual(found["etime_s"], 720)
        self.assertEqual(found["leaf_etime_s"], 700)
        self.assertEqual(found["kind"], "work")

    def test_wrapper_without_work_child_reports_itself(self):
        # A 12-minute `zsh` with nothing under it IS a 12-minute Bash tool call; inventing a
        # command name would be a guess, reporting the wrapper is the honest reading.
        tree, kids = tree_of([(100, 1, 3600, "claude"), (200, 100, 720, "zsh")])
        with no_helper_env():
            found = procscan.exec_child(100, tree, kids)
        self.assertEqual(found["comm"], "zsh")
        # Nothing is running under the call, so it reads as a wait — which is also why the
        # bare `zsh` name never reaches the screen (render shows `⏳ waiting <elapsed>`).
        self.assertEqual(found["kind"], "wait")

    def test_child_under_min_age_ignored(self):
        tree, kids = tree_of([(100, 1, 3600, "claude"), (200, 100, 59, "python3")])
        with no_helper_env():
            self.assertIsNone(procscan.exec_child(100, tree, kids))
        self.assertEqual(procscan.EXEC_MIN_AGE_SEC, model.SESSION_WORK_SEC)

    def test_harness_child_excluded(self):
        # A nested harness process already owns a row of its own (session / F-29 sub-agent).
        tree, kids = tree_of([(100, 1, 3600, "claude"), (200, 100, 720, "claude")])
        with no_helper_env():
            self.assertIsNone(procscan.exec_child(100, tree, kids))

    def test_internal_worker_env_marker_excluded(self):
        tree, kids = tree_of([(100, 1, 3600, "claude"), (200, 100, 720, "python3")])
        with mock.patch.object(procscan, "read_environ",
                               return_value={"MEM_DISTILL": "1"}):
            self.assertIsNone(procscan.exec_child(100, tree, kids))

    def test_boot_cohort_child_excluded(self):
        # Observed 2026-07-30: `node /tmp/steward-mcp-bridge.cjs` aged 237s inside a 238s
        # Codex session — a stdio MCP server, i.e. runtime plumbing that would otherwise
        # badge the row for the session's whole life.
        tree, kids = tree_of([(100, 1, 238, "codex"), (200, 100, 237, "node")])
        with no_helper_env():
            self.assertIsNone(procscan.exec_child(100, tree, kids))

    def test_child_launched_after_boot_still_counts(self):
        tree, kids = tree_of([(100, 1, 3600, "codex"), (200, 100, 720, "node")])
        with no_helper_env():
            self.assertEqual(procscan.exec_child(100, tree, kids)["comm"], "node")

    def test_boot_cohort_cut_never_hides_the_only_real_work(self):
        # Unknown session age (pid absent from the table) must not silently drop evidence.
        tree, kids = tree_of([(200, 100, 720, "python3")])
        with no_helper_env():
            self.assertEqual(procscan.exec_child(100, tree, kids)["comm"], "python3")

    def test_harness_companion_binary_excluded(self):
        tree, kids = tree_of([(100, 1, 3600, "codex"), (200, 100, 720, "codex-code-mode")])
        with no_helper_env():
            self.assertIsNone(procscan.exec_child(100, tree, kids))

    def test_longest_running_child_wins_deterministically(self):
        tree, kids = tree_of([(100, 1, 3600, "claude"),
                              (200, 100, 120, "npm"),
                              (300, 100, 900, "python3")])
        with no_helper_env():
            self.assertEqual(procscan.exec_child(100, tree, kids)["comm"], "python3")


class ShellCorrection(unittest.TestCase):
    """model.py — `shell` means "turn ended, a background shell job survives" (2026-09-16).

    v30 read it as "the Bash tool is running" and promoted `shell` + a long-lived child to
    working. Measured 2026-09-16: a foreground Bash call holds `busy` start to finish, and
    `shell` is written only after the turn ends. The promotion is gone; the badge carries
    the surviving job."""

    def setUp(self):
        model.reset_state_tracker()

    def evidence(self, status, exec_child=None, **over):
        ev = {"harness": "claude", "pid": 100, "pid_alive": True, "status": status,
              "mtime": 1000.0, "transcript": True, "exec_child": exec_child}
        ev.update(over)
        return ev

    def classify(self, ev, now=1200.0):
        return model.classify_session(ev, now)

    def test_shell_with_long_lived_child_is_idle_and_says_why(self):
        # 2026-09-16 premise correction: the turn is OVER, so the row must not read as working;
        # the job stays visible through the rule text and the badge.
        child = {"pid": 200, "comm": "python3", "etime_s": 720, "kind": "work"}
        state, ev = self.classify(self.evidence("shell", child))
        self.assertEqual(state, "idle")
        self.assertEqual(ev["tier"], 1)
        self.assertEqual(ev["source"], "claude-registry+proc")
        self.assertIn("turn ended", ev["rule"])
        self.assertIn("python3", ev["rule"])
        self.assertEqual(ev["raw_status"], "shell")

    def test_shell_without_child_stays_idle(self):
        state, ev = self.classify(self.evidence("shell"))
        self.assertEqual(state, "idle")
        self.assertEqual(ev["source"], "claude-registry")
        self.assertEqual(ev["rule"], "registry status=shell")

    def test_shell_child_predating_later_registry_activity_is_background(self):
        # Observed 2026-08-04: a 21h-old `tail -f` survived while later model turns
        # continued.  Claude kept registry status=shell, which must not make that old
        # background monitor the current foreground task.
        now = 5000.0
        child = {"pid": 200, "comm": "tail", "etime_s": 720}
        state, ev = self.classify(
            self.evidence("shell", child, updated_at=4401.0, mtime=4401.0), now=now
        )
        self.assertEqual(state, "idle")
        self.assertEqual(ev["source"], "claude-registry+proc")
        self.assertIn("turn ended", ev["rule"])
        self.assertIn("tail", ev["rule"])

    def test_shell_is_idle_whatever_the_registry_clock_skew(self):
        # The old rule needed a `updatedAt` gap to tell a live shell child from a background
        # one, so clock skew could flip the row. `shell` is idle either way now.
        now = 5000.0
        child = {"pid": 200, "comm": "python3", "etime_s": 720}
        for updated in (4330.0, 4401.0):
            model.reset_state_tracker()
            state, _ev = self.classify(
                self.evidence("shell", child, updated_at=updated, mtime=updated), now=now
            )
            self.assertEqual(state, "idle", updated)

    def test_idle_with_child_is_not_promoted(self):
        # Background case: the turn ended, the child outlived it. Model-side waiting is a
        # fact, so classification holds at idle and only the DISPLAY distinguishes it.
        child = {"pid": 200, "comm": "python3", "etime_s": 720}
        state, _ev = self.classify(self.evidence("idle", child))
        self.assertEqual(state, "idle")

    def test_busy_mapping_unchanged(self):
        for child in (None, {"pid": 200, "comm": "python3", "etime_s": 720}):
            model.reset_state_tracker()
            state, _ev = self.classify(self.evidence("busy", child))
            self.assertEqual(state, "working")

    def test_short_or_malformed_child_never_promotes(self):
        for child in ({"pid": 200, "comm": "python3", "etime_s": 59},
                      {"pid": 200, "comm": "", "etime_s": 720},
                      {"pid": 200, "etime_s": 720},
                      {"comm": "python3", "etime_s": None},
                      "python3", True):
            model.reset_state_tracker()
            state, _ev = self.classify(self.evidence("shell", child))
            self.assertEqual(state, "idle", "child=%r" % (child,))

    def test_stale_window_still_beats_a_shell_row_with_a_child(self):
        # The one documented tier-3-over-tier-1 exception (§4.8) must keep winning: a
        # 48h-silent session showing `working` is worse than showing it stale.
        child = {"pid": 200, "comm": "python3", "etime_s": 720}
        ev = self.evidence("shell", child, mtime=0.0)
        state, _e = model.classify_session(ev, now=model.SESSION_STALE_MIN * 60 * 3)
        self.assertEqual(state, "stale")

    def test_dead_process_still_terminates_the_row(self):
        child = {"pid": 200, "comm": "python3", "etime_s": 720}
        ev = self.evidence("shell", child, pid_alive=False)
        self.assertEqual(self.classify(ev)[0], "dead")

    def test_liveness_carries_exec_child_into_evidence(self):
        sess = Session(harness="claude", pid=100, status="shell",
                       exec_child={"pid": 200, "comm": "python3", "etime_s": 720})
        collected = liveness.collect_evidence(sess)
        self.assertEqual(collected["exec_child"]["comm"], "python3")
        with mock.patch.object(liveness, "_alive", return_value=True):
            self.assertEqual(liveness.classify(sess, now=1200.0), "idle")
        self.assertEqual(sess.state_evidence["source"], "claude-registry+proc")


class CodexExecDetail(unittest.TestCase):
    """collectors/codex.py — detail only; the lifecycle judge is untouched (prd.md:263)."""

    CALL = ('{"type":"response_item","payload":{"type":"custom_tool_call",'
            '"call_id":"call_A","name":"exec","input":"const r = await '
            'tools.exec_command({\\n  cmd: \'python3 train.py\'\\n})"}}')
    OUTPUT = ('{"type":"response_item","payload":{"type":"custom_tool_call_output",'
              '"call_id":"call_A","output":[]}}')
    STARTED = ('{"type":"event_msg","payload":{"type":"task_started",'
               '"turn_id":"turn-1"}}')
    COMPLETE = ('{"type":"event_msg","payload":{"type":"task_complete",'
                '"turn_id":"turn-1"}}')

    def rollout(self, *lines):
        path = os.path.join(self.dir, "rollout.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        return path

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_unclosed_tool_call_detected(self):
        detail = codex._tail_open_tool_call(self.rollout(self.STARTED, self.CALL))
        self.assertEqual(detail["name"], "exec")
        self.assertEqual(detail["command"], "python3")

    def test_paired_tool_call_is_closed(self):
        path = self.rollout(self.STARTED, self.CALL, self.OUTPUT)
        self.assertIsNone(codex._tail_open_tool_call(path))

    def test_malformed_and_missing_input_tolerated(self):
        path = self.rollout("not json", '{"payload": null}',
                            '{"type":"response_item","payload":{"type":"custom_tool_call"}}')
        self.assertIsNone(codex._tail_open_tool_call(path))
        self.assertIsNone(codex._tail_open_tool_call(os.path.join(self.dir, "absent.jsonl")))

    def test_lifecycle_judgment_unchanged_by_open_tool_call(self):
        # An unclosed call is NOT a state signal: a rollout ending task_complete stays idle
        # even with a dangling call_id, and task_started stays working without one.
        started = self.rollout(self.CALL, self.STARTED)
        self.assertEqual(codex._latest_task_lifecycle(started)[0], "task_started")
        done = self.rollout(self.STARTED, self.CALL, self.COMPLETE)
        # `rollout()` rewrites the same inode in place. The incremental observer must
        # fail closed for that truncation/rewrite observation, then initialize the
        # now-stable file on the next observation.
        self.assertIsNone(codex._latest_task_lifecycle(done))
        self.assertEqual(codex._latest_task_lifecycle(done)[0], "task_complete")
        model.reset_state_tracker()
        ev = {"harness": "codex", "pid": 100, "pid_alive": True, "status": None,
              "task_lifecycle": "task_complete", "mtime": 1000.0, "transcript": True,
              "exec_child": {"pid": 200, "comm": "python3", "etime_s": 720},
              "exec_tool": {"name": "exec", "command": "python3"}}
        state, evidence = model.classify_session(ev, 1200.0)
        self.assertEqual(state, "idle")
        self.assertEqual(evidence["source"], "codex-lifecycle")

    def test_managed_dir_join_moves_exec_child_to_visible_row(self):
        # The exec child hangs off the hidden app-server; v29's exact managed-dir join is
        # reused so the work surfaces on the visible `--remote` TUI row.
        mdir = "/tmp/managed-sessions/abc"
        server = Session(harness="codex", pid=100, cwd="/w", app_server=True,
                         managed_dir=mdir,
                         exec_child={"pid": 300, "comm": "python3", "etime_s": 720})
        client = Session(harness="codex", pid=200, cwd="/w", managed_dir=mdir)
        paths = {100: "/rollout.jsonl"}
        donated = codex._transfer_managed_rollouts([server, client], paths)
        self.assertEqual(donated, {100})
        self.assertEqual(client.exec_child["comm"], "python3")
        self.assertEqual(paths[200], "/rollout.jsonl")


class ExecDetailRender(unittest.TestCase):
    """render.py — the `⚙ <command> <elapsed>` detail in the NOW/summary zone."""

    def text_of(self, rows):
        return "".join(text for row in rows for text, _key in row)

    def keys_of(self, rows, needle):
        return [key for row in rows for text, key in row if needle in text]

    def test_working_row_shows_bright_detail(self):
        sess = Session(harness="claude", pid=100, liveness="working", ctx_pct=40,
                       exec_child={"pid": 200, "comm": "python3", "etime_s": 720})
        rows = render._context_detail_row(sess, term_width=200)
        self.assertIn("⚙ python3 12m", self.text_of(rows))
        self.assertEqual(self.keys_of(rows, "⚙"), ["g_work"])

    def test_background_experiment_under_idle_row_is_promoted(self):
        sess = Session(harness="claude", pid=100, liveness="idle", ctx_pct=40,
                       exec_child={"pid": 200, "comm": "python3", "etime_s": 720})
        rows = render._context_detail_row(sess, term_width=200)
        self.assertIn("⚙ python3 12m", self.text_of(rows))
        self.assertEqual(self.keys_of(rows, "⚙"), ["g_work"])

    def test_sub_minute_elapsed_uses_seconds(self):
        sess = Session(harness="claude", pid=100, liveness="working", ctx_pct=40,
                       exec_child={"pid": 200, "comm": "npm", "etime_s": 5})
        self.assertIn("⚙ npm 5s", self.text_of(render._context_detail_row(sess, term_width=200)))

    def test_codex_tool_detail_without_process_evidence(self):
        sess = Session(harness="codex", pid=100, liveness="working", ctx_pct=40,
                       exec_tool={"name": "exec", "command": "python3"})
        self.assertIn("⚙ exec", self.text_of(render._context_detail_row(sess, term_width=200)))

    def test_process_evidence_outranks_rollout_tool(self):
        sess = Session(harness="codex", pid=100, liveness="working", ctx_pct=40,
                       exec_child={"pid": 200, "comm": "python3", "etime_s": 720},
                       exec_tool={"name": "exec", "command": "python3"})
        text = self.text_of(render._context_detail_row(sess, term_width=200))
        self.assertIn("⚙ python3 12m", text)
        self.assertNotIn("exec", text)

    def test_badge_outlives_the_now_sentence_when_narrow(self):
        sess = Session(harness="claude", pid=100, liveness="working", ctx_pct=40,
                       exec_child={"pid": 200, "comm": "python3", "etime_s": 720},
                       summary="A" * 400)
        text = self.text_of(render._context_detail_row(sess, term_width=60))
        self.assertIn("⚙ python3 12m", text)
        self.assertNotIn("A" * 40, text)

    def test_no_evidence_renders_exactly_as_before(self):
        base = Session(harness="claude", pid=100, liveness="working", ctx_pct=40,
                       summary="doing something")
        self.assertIn("doing something", self.text_of(render._context_detail_row(base, term_width=200)))
        self.assertNotIn("⚙", self.text_of(render._context_detail_row(base, term_width=200)))

    def test_dead_row_shows_nothing(self):
        sess = Session(harness="claude", pid=100, liveness="dead", ctx_pct=40,
                       exec_child={"pid": 200, "comm": "python3", "etime_s": 720})
        self.assertEqual(render._context_detail_row(sess, term_width=200), [])


class WaitPrimitiveCorrection(unittest.TestCase):
    """v47 — a call with nothing under it is WAITING; 2026-09-16 — and it says so legibly.

    A poll loop's instantaneous sample almost always lands on its `sleep`, so promoting
    on it painted sleeping sessions green. The evidence stays visible; only the reading
    changes."""

    def setUp(self):
        model.reset_state_tracker()

    def evidence(self, status, exec_child=None, **over):
        ev = {"harness": "claude", "pid": 100, "pid_alive": True, "status": status,
              "mtime": 1000.0, "transcript": True, "exec_child": exec_child}
        ev.update(over)
        return ev

    def test_shell_with_sleep_child_stays_idle_with_an_honest_rule(self):
        child = {"pid": 200, "comm": "sleep", "etime_s": 240}
        state, ev = model.classify_session(self.evidence("shell", child), 1200.0)
        self.assertEqual(state, "idle")
        self.assertEqual(ev["source"], "claude-registry+proc")
        self.assertIn("turn ended", ev["rule"])
        self.assertIn("sleep", ev["rule"])

    def test_every_wait_primitive_reads_as_a_wait_in_evidence_and_badge(self):
        # Retargeted after review m1: `_session_status_state` now sends every `shell` row to
        # idle regardless of the child, so looping the vocabulary through it asserted
        # nothing — mutation check, flipping `exec_child_is_wait` to `return False` left the
        # old test green. Aim at the predicate and the badge, which is what the list drives.
        for comm in model.EXEC_WAIT_COMMS:
            child = {"pid": 200, "comm": comm, "etime_s": 720}
            self.assertTrue(model.exec_child_is_wait(child), comm)
            sess = Session(harness="claude", pid=100, liveness="idle", ctx_pct=40,
                           exec_child=child)
            rows = render._context_detail_row(sess, term_width=200)
            text = "".join(t for row in rows for t, _k in row)
            self.assertIn("⏳ waiting 12m", text, comm)
            self.assertNotRegex(text, r"\b" + re.escape(comm) + r"\b", comm)

    def test_busy_mapping_is_untouched_by_a_sleep_child(self):
        child = {"pid": 200, "comm": "sleep", "etime_s": 720}
        state, _ev = model.classify_session(self.evidence("busy", child), 1200.0)
        self.assertEqual(state, "working")

    def test_wait_badge_on_a_working_row_is_legible_and_never_names_the_primitive(self):
        # 2026-09-16: the row is working because the TURN is live (busy), not because of this
        # badge, so the badge is visible rather than dim — that is the whole point, the
        # user must be able to see "이 세션은 스크립트를 기다리는 중" at a glance. And it
        # names the wait, never `sleep`, whose own clock resets every iteration.
        sess = Session(harness="claude", pid=100, liveness="working", ctx_pct=40,
                       exec_child={"pid": 200, "comm": "sleep", "etime_s": 240,
                                   "kind": "wait"})
        rows = render._context_detail_row(sess, term_width=200)
        text = "".join(t for row in rows for t, _k in row)
        self.assertIn("⏳ waiting 4m", text)
        self.assertNotIn("sleep", text)
        self.assertNotIn("⚙", text)
        keys = [k for row in rows for t, k in row if "⏳" in t]
        self.assertEqual(keys, ["g_work"])

    def test_wait_badge_under_idle_row_is_dim(self):
        # The acceptance case the v47 rule exists for: a background poll loop under a
        # finished turn must not make the row — or its group — look hot.
        sess = Session(harness="claude", pid=100, liveness="idle", ctx_pct=40,
                       exec_child={"pid": 200, "comm": "sleep", "etime_s": 240})
        rows = render._context_detail_row(sess, term_width=200)
        text = "".join(t for row in rows for t, _k in row)
        self.assertIn("⏳ waiting 4m", text)
        keys = [k for row in rows for t, k in row if "⏳" in t]
        self.assertEqual(keys, ["dim"])


class CensusFollowsClassification(unittest.TestCase):
    """The badge is display only — it never inflates the working census (prd.md:263)."""

    def test_pulse_counts_background_child_as_idle(self):
        sessions = [
            Session(harness="claude", pid=100, liveness="idle",
                    exec_child={"pid": 200, "comm": "python3", "etime_s": 720}),
            Session(harness="claude", pid=300, liveness="working",
                    exec_child={"pid": 400, "comm": "npm", "etime_s": 720}),
        ]
        text = "".join(t for t, _k in render._pulse_segs(sessions, []))
        self.assertIn("1 working", text)
        self.assertIn("● 1 idle", text)

    def test_group_tier_counts_only_working_rows(self):
        sessions = [Session(harness="claude", pid=100, liveness="idle", cwd="/w",
                            exec_child={"pid": 200, "comm": "python3", "etime_s": 720})]
        self.assertEqual(sum(1 for s in sessions if s.liveness == "working"), 0)


class WaitingCallVisibility(unittest.TestCase):
    """2026-09-16 — "스크립트 돌려두고 기다리는 세션" reads correctly every tick.

    All four numbers below were measured 2026-09-16 against a live Claude Code 2.1.273
    session (pid 4013556), sampling `procscan.exec_child` every 0.25s:
      (a) `busy` resolved to working without consulting the wait evidence, so the glyph
          said work while the badge said wait, and a 40-minute wait looked like generation.
      (b) the ownership path returned None whenever the wrapper had no child right then —
          217 of 349 samples across a 100s wait — so the badge blinked out in the gaps.
      (c) label and elapsed came from the leaf: a 100s wait reported `sleep` at 0–1s, and a
          `sleep 20` poll loop can therefore never show a 9-minute wait.
      (d) the descent stopped at the first wait comm: `flock … timeout … python3` read as
          `⏳ flock` in 121/121 samples while python3 computed three levels below it.
    """

    def setUp(self):
        model.reset_state_tracker()

    def text_of(self, rows):
        return "".join(text for row in rows for text, _key in row)

    def owned_scan(self, rows, ids, expected_start):
        """`exec_child` down the ownership-verified path Claude sessions always take."""
        tree, kids = tree_of(rows)
        with mock.patch.object(procscan, "_exec_identity", side_effect=ids.get), \
             mock.patch.object(procscan, "read_environ", return_value={}):
            return procscan.exec_child(100, tree, kids, expected_start=expected_start)

    # (c) elapsed -------------------------------------------------------------------------
    def test_poll_loop_reports_the_calls_elapsed_not_the_sleeps(self):
        # 9 minutes into a `sleep 20` loop: the leaf is 3s old and always will be.
        tree, kids = tree_of([(100, 1, 3600, "claude"),
                              (200, 100, 540, "zsh"),
                              (300, 200, 3, "sleep")])
        with no_helper_env():
            found = procscan.exec_child(100, tree, kids)
        self.assertEqual(found["etime_s"], 540)      # the call — what the user asked to see
        self.assertEqual(found["leaf_etime_s"], 3)   # the sleep — kept for forensics only
        self.assertEqual(found["kind"], "wait")
        sess = Session(harness="claude", pid=100, liveness="working", ctx_pct=40,
                       exec_child=found)
        text = self.text_of(render._context_detail_row(sess, term_width=200))
        self.assertIn("⏳ waiting 9m", text)
        self.assertNotIn("sleep", text)

    # (b) stability -----------------------------------------------------------------------
    def test_badge_survives_the_gap_between_loop_iterations(self):
        # Between two `sleep`s the wrapper momentarily has no child. Before the fix the
        # ownership path returned None here and the badge vanished for that tick.
        ids = {100: (1, "900"), 200: (100, "901")}
        found = self.owned_scan([(100, 1, 3600, "claude"), (200, 100, 540, "zsh")],
                                ids, expected_start="900")
        self.assertIsNotNone(found)
        self.assertEqual(found["kind"], "wait")
        self.assertEqual(found["etime_s"], 540)
        self.assertTrue(found["ownership_verified"])

    def test_the_elapsed_is_identical_mid_sleep_and_in_the_gap(self):
        # The badge must not jitter as the loop cycles: same call, same elapsed, same kind.
        ids = {100: (1, "900"), 200: (100, "901"), 300: (200, "902")}
        mid = self.owned_scan([(100, 1, 3600, "claude"), (200, 100, 540, "zsh"),
                               (300, 200, 7, "sleep")], ids, expected_start="900")
        gap = self.owned_scan([(100, 1, 3600, "claude"), (200, 100, 540, "zsh")],
                              ids, expected_start="900")
        for field in ("etime_s", "kind", "child_pid"):
            self.assertEqual(mid[field], gap[field], field)

    # (d) guards --------------------------------------------------------------------------
    def test_guard_chain_with_a_workload_under_it_is_work(self):
        # `flock -w 1500 … timeout 1500 node …` — measured on cairn-91, 226s of real work
        # that rendered as `⏳ flock`. A guard with something under it IS that something.
        tree, kids = tree_of([(100, 1, 3600, "claude"),
                              (200, 100, 240, "zsh"),
                              (300, 200, 239, "flock"),
                              (400, 300, 239, "timeout"),
                              (500, 400, 238, "python3")])
        with no_helper_env():
            found = procscan.exec_child(100, tree, kids)
        self.assertEqual(found["comm"], "python3")
        self.assertEqual(found["kind"], "work")
        self.assertEqual(found["etime_s"], 240)
        sess = Session(harness="claude", pid=100, liveness="working", ctx_pct=40,
                       exec_child=found)
        self.assertIn("⚙ python3 4m", self.text_of(render._context_detail_row(sess, term_width=200)))

    def test_a_guard_with_nothing_under_it_really_is_a_wait(self):
        # The other half of the same rule: `flock` blocked on a lock has no child, and that
        # is a genuine wait — the rule must not simply stop calling guards waits.
        tree, kids = tree_of([(100, 1, 3600, "claude"),
                              (200, 100, 240, "zsh"),
                              (300, 200, 239, "flock")])
        with no_helper_env():
            found = procscan.exec_child(100, tree, kids)
        self.assertEqual(found["comm"], "flock")
        self.assertEqual(found["kind"], "wait")

    # (a) the two readings are distinguishable --------------------------------------------
    def test_a_busy_session_waiting_is_distinguishable_from_one_working(self):
        waiting = {"pid": 300, "comm": "sleep", "etime_s": 540, "kind": "wait"}
        running = {"pid": 300, "comm": "python3", "etime_s": 540, "kind": "work"}
        seen = {}
        for name, child in (("wait", waiting), ("work", running)):
            model.reset_state_tracker()
            ev = {"harness": "claude", "pid": 100, "pid_alive": True, "status": "busy",
                  "mtime": 1000.0, "transcript": True, "exec_child": child}
            state, evidence = model.classify_session(ev, 1200.0)
            # The turn IS live in both cases, so the activity axis stays `working` — the
            # distinction is carried by the evidence rule and the badge, not by a state.
            self.assertEqual(state, "working", name)
            sess = Session(harness="claude", pid=100, liveness="working", ctx_pct=40,
                           exec_child=child)
            seen[name] = (evidence["rule"],
                          self.text_of(render._context_detail_row(sess, term_width=200)))
        self.assertIn("waiting", seen["wait"][0])
        self.assertIn("running", seen["work"][0])
        self.assertNotEqual(seen["wait"][0], seen["work"][0])
        self.assertIn("⏳ waiting 9m", seen["wait"][1])
        self.assertIn("⚙ python3 9m", seen["work"][1])

    # thresholds and neighbours preserved -------------------------------------------------
    def test_a_call_under_the_flicker_threshold_is_still_invisible(self):
        tree, kids = tree_of([(100, 1, 3600, "claude"),
                              (200, 100, 59, "zsh"),
                              (300, 200, 3, "sleep")])
        with no_helper_env():
            self.assertIsNone(procscan.exec_child(100, tree, kids))

    def test_sandbox_plumbing_with_nothing_under_it_is_still_no_evidence(self):
        # Widening "a bare wrapper is the call" must not start badging rows for the
        # runtime's own sandbox scaffolding mid-setup.
        tree, kids = tree_of([(100, 1, 3600, "codex"), (200, 100, 240, "bwrap")])
        with no_helper_env():
            self.assertIsNone(procscan.exec_child(100, tree, kids))

    def test_codex_rollout_badge_is_untouched(self):
        sess = Session(harness="codex", pid=100, liveness="working", ctx_pct=40,
                       exec_tool={"name": "exec", "command": "python3"})
        self.assertIn("⚙ exec", self.text_of(render._context_detail_row(sess, term_width=200)))

    # regressions found by the 2026-09-16 independent review ------------------------------
    def test_a_background_poll_loop_does_not_oscillate(self):
        """Review B1 — the blocking regression this cycle introduced and then fixed.

        `exec_child_evidence`'s >=60s gate used to land on the LEAF's age, and that is what
        held `owned_exec_work` (tier 2, decided before the registry) still while a poll loop
        alternated between its body and its `sleep`. Moving `etime_s` to the call made every
        3-second loop body clear that gate, so the row flipped working/idle every tick and
        `_group_activity_rank` dragged the project card with it. The leaf now has its own
        gate, so the shape below is stably idle again — as it is on main.
        """
        base = dict(proc_start="208", root_pid=100, root_start="200",
                    ownership_verified=True, attempt_id=None,
                    ancestry=[dict(pid=100, ppid=1, start="200"),
                              dict(pid=200, ppid=100, start="208")])
        body = dict(base, pid=200, comm="curl", etime_s=900, leaf_etime_s=3, kind="work")
        nap = dict(base, pid=200, comm="sleep", etime_s=900, leaf_etime_s=2, kind="wait")
        seen = []
        for child in (body, nap, body, nap, body):
            model.reset_state_tracker()
            # `updated_at` = the turn ending ~5s after the loop was backgrounded, which is
            # what `run_in_background` looks like — and it keeps `exec_child_is_background`
            # False forever, so nothing else damps the oscillation.
            ev = {"harness": "claude", "pid": 100, "pid_alive": True, "status": "shell",
                  "proc_start": "200", "mtime": 1000.0, "transcript": True,
                  "exec_child": child, "updated_at": 305.0}
            seen.append(model.classify_session(ev, 1200.0)[0])
        self.assertEqual(set(seen), {"idle"}, seen)

    def test_a_real_long_running_workload_still_counts_as_work(self):
        # The other side of the same gate: the leaf test must not silence genuine work.
        child = dict(pid=200, comm="python3", etime_s=900, leaf_etime_s=880, kind="work",
                     proc_start="208", root_pid=100, root_start="200",
                     ownership_verified=True, attempt_id=None,
                     ancestry=[dict(pid=100, ppid=1, start="200"),
                               dict(pid=200, ppid=100, start="208")])
        ev = {"harness": "claude", "pid": 100, "pid_alive": True, "status": "shell",
              "proc_start": "200", "mtime": 1000.0, "transcript": True,
              "exec_child": child, "updated_at": 305.0}
        state, evidence = model.classify_session(ev, 1200.0)
        self.assertEqual(state, "working")
        self.assertEqual(evidence["source"], "owned-exec")

    def test_evidence_without_a_leaf_age_falls_back_to_the_call(self):
        # `--json` replays and fixtures predate `leaf_etime_s`; they must keep working.
        child = dict(pid=200, comm="node", etime_s=120, proc_start="208",
                     root_pid=100, root_start="200", ownership_verified=True,
                     attempt_id=None,
                     ancestry=[dict(pid=100, ppid=1, start="200"),
                               dict(pid=200, ppid=100, start="208")])
        self.assertEqual(model.exec_child_work_age(child), 120)
        ev = {"harness": "claude", "pid": 100, "pid_alive": True, "status": "shell",
              "proc_start": "200", "mtime": 1000.0, "transcript": True,
              "exec_child": child, "updated_at": 305.0}
        self.assertEqual(model.classify_session(ev, 1200.0)[0], "working")

    def test_shell_rule_never_pairs_a_leaf_name_with_the_calls_clock(self):
        # Review r2 N2: a 3-second `curl` inside a 900-second loop was described as
        # "curl (900s) still alive". Each number must belong to the process it is next to.
        child = {"pid": 300, "comm": "curl", "etime_s": 900, "leaf_etime_s": 3, "kind": "work"}
        ev = {"harness": "claude", "pid": 100, "pid_alive": True, "status": "shell",
              "mtime": 1000.0, "transcript": True, "exec_child": child}
        _state, evidence = model.classify_session(ev, 1200.0)
        self.assertIn("call 900s", evidence["rule"])
        self.assertIn("curl (3s)", evidence["rule"])
        self.assertNotIn("curl (900s)", evidence["rule"])

    def test_ownership_verified_child_still_decides_before_the_registry(self):
        # Review M1 — pin what production actually does rather than what the tier-1
        # docstring reads like on its own: `procscan.scan` always collects ownership
        # evidence, and `owned_exec_work` is consulted before the registry status, so
        # `shell` is idle BY THE REGISTRY, not unconditionally. main behaves identically;
        # this is a documented gap, not a regression, and the test exists so a future
        # reader cannot mistake the narrower claim for the whole contract.
        child = dict(pid=200, comm="python3", etime_s=720, leaf_etime_s=720, kind="work",
                     proc_start="208", root_pid=100, root_start="200",
                     ownership_verified=True, attempt_id=None,
                     ancestry=[dict(pid=100, ppid=1, start="200"),
                               dict(pid=200, ppid=100, start="208")])
        ev = {"harness": "claude", "pid": 100, "pid_alive": True, "status": "shell",
              "proc_start": "200", "mtime": 1000.0, "transcript": True,
              "exec_child": child, "updated_at": 500.0}
        state, evidence = model.classify_session(ev, 1200.0)
        self.assertEqual(state, "working")
        self.assertEqual(evidence["source"], "owned-exec")


if __name__ == "__main__":
    unittest.main()
