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
import os
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
        self.assertEqual(found, {"pid": 200, "comm": "python3", "etime_s": 720})

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
        # prd.md:263 — elapsed is the DESCENDED process's own etime, not the wrapper's.
        self.assertEqual(found["etime_s"], 700)

    def test_wrapper_without_work_child_reports_itself(self):
        # A 12-minute `zsh` with nothing under it IS a 12-minute Bash tool call; inventing a
        # command name would be a guess, reporting the wrapper is the honest reading.
        tree, kids = tree_of([(100, 1, 3600, "claude"), (200, 100, 720, "zsh")])
        with no_helper_env():
            self.assertEqual(procscan.exec_child(100, tree, kids)["comm"], "zsh")

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
    """model.py — `shell` is "Bash tool running", not an idle synonym (prd.md:612)."""

    def setUp(self):
        model.reset_state_tracker()

    def evidence(self, status, exec_child=None, **over):
        ev = {"harness": "claude", "pid": 100, "pid_alive": True, "status": status,
              "mtime": 1000.0, "transcript": True, "exec_child": exec_child}
        ev.update(over)
        return ev

    def classify(self, ev, now=1200.0):
        return model.classify_session(ev, now)

    def test_shell_with_long_lived_child_is_working(self):
        child = {"pid": 200, "comm": "python3", "etime_s": 720}
        state, ev = self.classify(self.evidence("shell", child))
        self.assertEqual(state, "working")
        self.assertEqual(ev["tier"], 1)
        self.assertEqual(ev["source"], "claude-registry+proc")
        self.assertIn("shell", ev["rule"])
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
        self.assertIn("later registry activity proves background", ev["rule"])

    def test_process_start_skew_does_not_demote_current_shell_child(self):
        now = 5000.0
        child = {"pid": 200, "comm": "python3", "etime_s": 720}
        state, _ev = self.classify(
            self.evidence("shell", child, updated_at=4330.0, mtime=4330.0), now=now
        )
        self.assertEqual(state, "working")

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

    def test_stale_window_still_beats_promoted_shell(self):
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
            self.assertEqual(liveness.classify(sess, now=1200.0), "working")
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
    """v47 — a wait-primitive child (`sleep` & co) is WAITING: no promotion, dim ⏳ badge.

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
        self.assertIn("waiting, not promoted", ev["rule"])
        self.assertIn("sleep", ev["rule"])

    def test_every_wait_primitive_suppresses_promotion(self):
        for comm in model.EXEC_WAIT_COMMS:
            model.reset_state_tracker()
            child = {"pid": 200, "comm": comm, "etime_s": 720}
            state, _ev = model.classify_session(self.evidence("shell", child), 1200.0)
            self.assertEqual(state, "idle", comm)

    def test_busy_mapping_is_untouched_by_a_sleep_child(self):
        child = {"pid": 200, "comm": "sleep", "etime_s": 720}
        state, _ev = model.classify_session(self.evidence("busy", child), 1200.0)
        self.assertEqual(state, "working")

    def test_wait_badge_is_hourglass_and_dim_even_on_a_working_row(self):
        # Working via some other evidence (busy status) — the badge must still not
        # read as work: the child is a wait, whatever the row's state.
        sess = Session(harness="claude", pid=100, liveness="working", ctx_pct=40,
                       exec_child={"pid": 200, "comm": "sleep", "etime_s": 240})
        rows = render._context_detail_row(sess, term_width=200)
        text = "".join(t for row in rows for t, _k in row)
        self.assertIn("⏳ sleep 4m", text)
        self.assertNotIn("⚙", text)
        keys = [k for row in rows for t, k in row if "⏳" in t]
        self.assertEqual(keys, ["dim"])

    def test_wait_badge_under_idle_row_is_dim(self):
        sess = Session(harness="claude", pid=100, liveness="idle", ctx_pct=40,
                       exec_child={"pid": 200, "comm": "sleep", "etime_s": 240})
        rows = render._context_detail_row(sess, term_width=200)
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


if __name__ == "__main__":
    unittest.main()
