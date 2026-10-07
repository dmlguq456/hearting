"""F-104: live GPU work that no run registry or session line shows gets a card row."""

import contextlib
import io
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock


TOOLS = Path(__file__).resolve().parents[2]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from fleet import fleet, render  # noqa: E402
from fleet.collectors import compute_hosts  # noqa: E402
from fleet.model import DispatchJob, ResourceJob, Session, fmt_min  # noqa: E402


SR_CWD = "/home/nas/user/Uihyeop/NN_Zoo/SR_CorrNet_DSC"
M6_COMMAND = ("/home/nas/env/bin/python run.py --engine_mode train "
              "--config _m6_lx3nx4.yaml --gpuid 0")
SESSION_ID = "01a0e597-1111-2222-3333-444455556666"


def _process(pid=3697573, **overrides):
    row = {
        "pid": pid, "proc_start": 123456, "pgid": 4242, "process_name": "python",
        "used_memory_mib": 19208, "command": M6_COMMAND, "cwd": SR_CWD,
        "elapsed_s": 4500 * 60, "owner": None, "attribution_reason": None,
    }
    row.update(overrides)
    return row


def _snapshot(*gpus, host="moving4", is_self=True, reachable=True):
    return {"configured": True, "observed_at": time.time(), "hosts": [{
        "host": host, "self": is_self, "reachable": reachable, "gpus": [
            {"index": index, "name": "NVIDIA RTX A6000", "processes": list(processes)}
            for index, processes in gpus],
    }]}


def _session_owner(harness="codex", sid=SESSION_ID):
    return {"kind": "session", "harness": harness, "id": sid,
            "source": "persistent-claim+ancestry"}


class UnregisteredGpuTestBase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(render.set_compute_hosts, None)
        self.addCleanup(setattr, render, "_BLINK_ON", render._BLINK_ON)

    def lines(self, snapshot, sessions=(), section="both", width=120, resources=None):
        render.set_compute_hosts(snapshot)
        built = render._build_lines(list(sessions), [], section, False, 0,
                                    term_width=width, resources=resources)
        return [render._plain(line) if line is not None else "" for line in built]


class UnregisteredGpuRowTest(UnregisteredGpuTestBase):
    def test_unregistered_process_renders_in_its_project_card(self):
        text = self.lines(_snapshot((0, [_process()])))
        header = next(i for i, line in enumerate(text) if "SR_CorrNet_DSC/" in line)
        row = next(i for i, line in enumerate(text) if "GPU moving4:0" in line)
        self.assertGreater(row, header)
        for token in ("M6 학습", "19 GB", fmt_min(4500), "미등록"):
            self.assertIn(token, text[row])
        self.assertIn("●", text[header])

        entry = compute_hosts.unregistered_gpu(_snapshot((0, [_process()])))[0]
        group = {"sessions": [], "jobs": [], "gpu": [entry]}
        emission = render._group_emission(group, True, True)
        self.assertIs(emission["fold"], False)
        self.assertIs(emission["empty"], False)
        self.assertEqual(emission["gpu"], [entry])

    def test_registered_run_is_not_repeated_by_pid_or_process_group(self):
        snapshot = _snapshot((0, [_process(pid=500, pgid=490)]))

        def run(**kw):
            base = dict(run_id="r1", liveness="working", pid=488, starttime="111",
                        process_group=490)
            base.update(kw)
            return ResourceJob(**base)

        self.assertEqual(compute_hosts.unregistered_gpu(snapshot, [run()]), [])
        same_pid = run(pid=500, starttime="123456", process_group=None)
        self.assertEqual(compute_hosts.unregistered_gpu(snapshot, [same_pid]), [])
        reused_pid = run(pid=500, starttime="999", process_group=None)
        self.assertEqual(len(compute_hosts.unregistered_gpu(snapshot, [reused_pid])), 1)
        self.assertEqual(
            len(compute_hosts.unregistered_gpu(snapshot, [run(liveness="exited")])), 1)
        remote = _snapshot((0, [_process(pid=500, pgid=490)]), is_self=False)
        self.assertEqual(len(compute_hosts.unregistered_gpu(remote, [run()])), 1)

    def test_process_shown_in_session_strip_is_not_repeated(self):
        owner = _session_owner()
        snapshot = _snapshot((0, [_process(owner=owner, session_owner=owner)]))
        session = Session(harness="codex", pid=101, proc_start="11", cwd=SR_CWD,
                          session_id=SESSION_ID, title="training", liveness="working")
        text = self.lines(snapshot, [session])
        self.assertEqual(sum(line.count("● GPU") for line in text), 1)
        row = next(line for line in text if "● GPU" in line)
        self.assertNotIn("미등록", row)

    def test_process_whose_session_left_the_board_gets_a_row(self):
        owner = _session_owner()
        snapshot = _snapshot((0, [_process(owner=owner, session_owner=owner)]))
        text = self.lines(snapshot)
        rows = [line for line in text if "● GPU moving4:0" in line]
        self.assertEqual(len(rows), 1)
        self.assertIn("미등록", rows[0])

    def test_probe_failure_is_silent_and_empty(self):
        good = _snapshot((0, [_process()]))
        cases = [
            {"configured": True, "hosts": [], "error": "compute-host probe timed out"},
            {"configured": False, "status": "missing", "hosts": []},
            _snapshot((0, [_process()]), reachable=False),
            {"configured": True, "hosts": [{"host": "h", "reachable": True, "gpus": {}}]},
            {"configured": True, "hosts": [{"host": "h", "reachable": True,
                                            "gpus": [{"index": 0, "processes": "x"}]}]},
            _snapshot((0, [_process(pid=True, pgid=None)])),
            _snapshot((0, [_process(pid="12", pgid=None)])),
            _snapshot((0, ["junk", None, 7])),
        ]
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            for case in cases:
                self.assertEqual(compute_hosts.unregistered_gpu(case), [], case)
                self.assertNotIn("● GPU", "\n".join(self.lines(case)))
            for junk in (None, [], "text", 5):
                self.assertEqual(compute_hosts.unregistered_gpu(junk), [])
            render.set_compute_hosts(good)
            with mock.patch.object(render, "_COMPUTE_HOSTS_SET_AT", time.monotonic() - 31):
                stale = render._build_lines([], [], "both", False, 0, term_width=120)
            self.assertNotIn("● GPU", "\n".join(render._plain(x) for x in stale if x))
        self.assertEqual(stderr.getvalue(), "")

    def test_unreadable_cwd_goes_to_unknown_project(self):
        for cwd in (None, "relative/path", ""):
            with self.subTest(cwd=cwd):
                text = self.lines(_snapshot((0, [_process(cwd=cwd)])))
                header = next(i for i, line in enumerate(text) if "(unknown)/" in line)
                row = next(i for i, line in enumerate(text) if "GPU moving4:0" in line)
                self.assertGreater(row, header)
                self.assertIn("미등록", text[row])

    def test_row_never_overflows_and_keeps_identity(self):
        entry = compute_hosts.unregistered_gpu(_snapshot((0, [_process(
            command="/home/test/envs/xxx/bin/python run.py  --flag '/keep/full path'"
        )])))[0]
        original = json.dumps(entry, sort_keys=True)
        for width in (168, 100, 60, 40):
            row = render._plain(render._gpu_work_row(entry, width))
            self.assertLessEqual(render._dw(row), width, row)
            self.assertIn("GPU moving4:0", row)
            self.assertNotIn("/home/test/envs/xxx/bin/python", row)
            if width == 168:
                self.assertIn("python run.py --flag 'full path'", row)
            self.assertEqual(json.dumps(entry, sort_keys=True), original)

        quoted = compute_hosts.unregistered_gpu(_snapshot((0, [_process(
            command='"/env space/bin/python" run.py --config "/cfg space/m6.json"'
        )])))[0]
        original_quoted = json.dumps(quoted, sort_keys=True)
        quoted_row = render._plain(render._gpu_work_row(quoted, 168))
        self.assertIn(" · M6 · ", quoted_row)
        self.assertEqual(json.dumps(quoted, sort_keys=True), original_quoted)

    def test_long_argv_tail_survives_available_width(self):
        # Original case (C-PR176 live observation on cnn): a long env python
        # plus a long script path with no --run-id/--name/--config identifier.
        # Path prefixes must not hide the script, even in the narrower row.
        entry = compute_hosts.unregistered_gpu(_snapshot((0, [_process(
            command="/home/nas/user/Uihyeop/NN_Zoo/TF-Rehancer_artifacts/envs/"
                     "private_cnn_cu128_20261006/bin/python "
                     "/home/nas/user/Uihyeop/NN_Zoo/TF-Rehancer_artifacts/envs/"
                     "private_cnn_train.py",
            used_memory_mib=4915, elapsed_s=8 * 3600 + 47 * 60,
        )])))[0]
        original = json.dumps(entry, sort_keys=True)
        wide = render._plain(render._gpu_work_row(entry, 168))
        self.assertLessEqual(render._dw(wide), 168)
        self.assertIn("python private_cnn_train.py", wide)
        self.assertNotIn("…", wide)
        # Identifier-based labels (M6/config) keep their existing short form.
        m6 = render._plain(render._gpu_work_row(
            compute_hosts.unregistered_gpu(_snapshot((0, [_process()])))[0], 168))
        self.assertIn("M6 학습", m6)
        # Filename compaction leaves the script visible at the narrower width.
        narrow = render._plain(render._gpu_work_row(entry, 60))
        self.assertLessEqual(render._dw(narrow), 60)
        self.assertIn("GPU moving4:0", narrow)
        self.assertIn("private_cnn_train.py", narrow)
        self.assertEqual(json.dumps(entry, sort_keys=True), original)

    def test_multi_gpu_process_is_one_row_and_dispatch_section_only(self):
        shared = _process(used_memory_mib=9604)
        snapshot = _snapshot((0, [shared]), (1, [dict(shared)]), host="cnn", is_self=False)
        entries = compute_hosts.unregistered_gpu(snapshot)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["gpu_indexes"], [0, 1])
        self.assertEqual(entries[0]["used_memory_mib"], 19208)
        text = self.lines(snapshot)
        rows = [line for line in text if "GPU cnn:0,1" in line]
        self.assertEqual(len(rows), 1)
        self.assertIn("19 GB", rows[0])
        self.assertNotIn("● GPU", "\n".join(self.lines(snapshot, section="fleet")))

    def test_group_with_gpu_session_never_folds_and_shows_gpu_once(self):
        owner = _session_owner()
        snapshot = _snapshot((0, [_process(owner=owner, session_owner=owner)]))
        session = Session(harness="codex", pid=101, proc_start="11", cwd=SR_CWD,
                          session_id=SESSION_ID, title="managed", liveness="working",
                          app_server=True)
        session._managed_client_present = False
        render.set_compute_hosts(snapshot)
        gpu_resources = render._gpu_session_resources()
        group = {"sessions": [session], "jobs": []}
        emission = render._group_emission(group, True, True, gpu_resources)
        self.assertIs(emission["fold"], False)
        self.assertEqual(emission["gpu_strip_keys"], {("codex", SESSION_ID)})
        text = self.lines(snapshot, [session])
        self.assertEqual(sum(line.count("● GPU") for line in text), 1)
        self.assertNotIn("미등록", "\n".join(text))
        quiet = render._group_emission(group, True, True, {})
        self.assertIs(quiet["fold"], True)

    def test_mem_worker_owned_gpu_gets_a_card_row(self):
        owner = _session_owner()
        snapshot = _snapshot((0, [_process(owner=owner, session_owner=owner)]))
        mem = Session(harness="codex", pid=101, proc_start="11", cwd=SR_CWD,
                      session_id=SESSION_ID, title="mem", liveness="working")
        mem.mem_worker = True
        render.set_compute_hosts(snapshot)
        gpu_resources = render._gpu_session_resources()
        self.assertEqual(render._gpu_strip_keys([mem], gpu_resources), set())
        text = self.lines(snapshot, [mem])
        self.assertEqual(sum(line.count("● GPU") for line in text), 1)
        header = next(i for i, line in enumerate(text) if "SR_CorrNet_DSC/" in line)
        row = next(i for i, line in enumerate(text) if "● GPU moving4:0" in line)
        self.assertGreater(row, header)

    def _job_owned_gpu(self, **job_overrides):
        owner = _session_owner()
        snapshot = _snapshot((0, [_process(owner=owner, session_owner=owner)]))
        # The dispatch child's own session is hidden (is_child); its job row draws the strip.
        child = Session(harness="codex", pid=101, proc_start="11", cwd=SR_CWD,
                        session_id=SESSION_ID, title="worker", liveness="working",
                        is_child=True)
        fields = dict(key="autopilot-code", slug="job1", cwd=SR_CWD, harness="codex",
                      is_child=True, liveness="working")
        fields.update(job_overrides)
        job = DispatchJob(**fields)
        job._runtime_session_id = SESSION_ID
        return snapshot, child, job

    def gpu_text(self, snapshot, sessions, jobs):
        render.set_compute_hosts(snapshot)
        built = render._build_lines(list(sessions), list(jobs), "both", False, 0,
                                    term_width=120)
        return [render._plain(line) if line is not None else "" for line in built]

    def test_process_shown_in_dispatch_job_strip_is_not_repeated(self):
        snapshot, child, job = self._job_owned_gpu()
        text = self.gpu_text(snapshot, [child], [job])
        self.assertEqual(sum(line.count("● GPU") for line in text), 1)
        self.assertNotIn("미등록", "\n".join(text))
        # the same holds when the hidden child session is not in the snapshot at all
        text = self.gpu_text(snapshot, [], [job])
        self.assertEqual(sum(line.count("● GPU") for line in text), 1)
        self.assertNotIn("미등록", "\n".join(text))

    def _group_keys(self, snapshot, sessions, jobs):
        render.set_compute_hosts(snapshot)
        gpu_resources = render._gpu_session_resources()
        group = {"sessions": list(sessions), "jobs": list(jobs)}
        return render._group_emission(group, True, True, gpu_resources)["gpu_strip_keys"]

    def test_job_strip_key_follows_the_drawn_job_only(self):
        snapshot, _child, job = self._job_owned_gpu()
        self.assertEqual(self._group_keys(snapshot, [], [job]), {("codex", SESSION_ID)})
        owner = DispatchJob(key="autopilot-code", slug="own", cwd=SR_CWD, harness="codex",
                            liveness="working", depth=1)
        folded = DispatchJob(key="autopilot-code", slug="stage", cwd=SR_CWD, harness="codex",
                             liveness="done", depth=2, parent_slug="own")
        folded._runtime_session_id = SESSION_ID
        self.assertEqual(self._group_keys(snapshot, [], [owner, folded]), set())

    def _dead_owner_job(self, **overrides):
        fields = dict(key="autopilot-code", slug="dead-owner", cwd=SR_CWD, harness="codex",
                      liveness="dead", note="dead-runtime-exit")
        fields.update(overrides)
        job = DispatchJob(**fields)
        job._runtime_session_id = SESSION_ID
        job._dead_terminal_owner = True
        return job

    def _gpu_snapshot(self):
        owner = _session_owner()
        return _snapshot((0, [_process(owner=owner, session_owner=owner)]))

    def _texts(self, snapshot, sessions, jobs, show_all):
        self.addCleanup(setattr, render, "_SHOW_ALL", render._SHOW_ALL)
        render._SHOW_ALL = show_all
        return self.gpu_text(snapshot, sessions, jobs)

    def test_dropped_dead_owner_orphan_leaves_the_card_row_to_show_the_process(self):
        snapshot = self._gpu_snapshot()
        job = self._dead_owner_job()
        text = self._texts(snapshot, [], [job], show_all=False)
        rows = [line for line in text if "● GPU" in line]
        self.assertEqual(len(rows), 1)
        self.assertIn("미등록", rows[0])
        self.assertEqual(self._group_keys(snapshot, [], [job]), set())
        # --all draws the job row and its strip, so the card row steps aside
        text = self._texts(snapshot, [], [job], show_all=True)
        rows = [line for line in text if "● GPU" in line]
        self.assertEqual(len(rows), 1)
        self.assertNotIn("미등록", "\n".join(text))

    def test_nested_dead_owner_job_draws_its_strip_so_the_process_shows_once(self):
        snapshot = self._gpu_snapshot()
        parent = Session(harness="claude", pid=201, proc_start="21", cwd=SR_CWD,
                         session_id="parent-session", title="lead", liveness="working")
        job = self._dead_owner_job(is_child=True)
        job.parent_sid = parent.session_id
        job.parent_cwd = SR_CWD
        for show_all in (False, True):
            text = self._texts(snapshot, [parent], [job], show_all=show_all)
            self.assertEqual(sum(line.count("● GPU") for line in text), 1, show_all)
            self.assertNotIn("미등록", "\n".join(text))

    def test_children_of_a_duplicate_session_id_draw_once_or_fall_back_to_the_card(self):
        snapshot = self._gpu_snapshot()

        def session(pid, sid="dup-session"):
            return Session(harness="claude", pid=pid, proc_start=str(pid), cwd=SR_CWD,
                           session_id=sid, title="lead", liveness="working")

        job = DispatchJob(key="autopilot-code", slug="job1", cwd=SR_CWD, harness="codex",
                          liveness="working", is_child=True)
        job._runtime_session_id = SESSION_ID
        job.parent_sid = "dup-session"
        text = self._texts(snapshot, [session(301), session(302)], [job], show_all=False)
        self.assertEqual(sum(line.count("● GPU") for line in text), 1)
        self.assertNotIn("미등록", "\n".join(text))

    def test_children_of_a_mem_worker_session_are_dropped_so_the_card_row_shows_it(self):
        snapshot = self._gpu_snapshot()
        mem = Session(harness="claude", pid=401, proc_start="41", cwd=SR_CWD,
                      session_id="mem-session", title="mem", liveness="working")
        mem.mem_worker = True
        job = DispatchJob(key="autopilot-code", slug="job1", cwd=SR_CWD, harness="codex",
                          liveness="working", is_child=True)
        job._runtime_session_id = SESSION_ID
        job.parent_sid = "mem-session"
        text = self._texts(snapshot, [mem], [job], show_all=False)
        rows = [line for line in text if "● GPU" in line]
        self.assertEqual(len(rows), 1)
        self.assertIn("미등록", rows[0])

    def test_folded_job_draws_no_strip_so_the_card_row_shows_it_once(self):
        snapshot, _child, _job = self._job_owned_gpu()
        owner = DispatchJob(key="autopilot-code", slug="own", cwd=SR_CWD, harness="codex",
                            liveness="working", depth=1)
        folded = DispatchJob(key="autopilot-code", slug="stage", cwd=SR_CWD, harness="codex",
                             liveness="done", depth=2, parent_slug="own")
        folded._runtime_session_id = SESSION_ID
        text = self.gpu_text(snapshot, [], [owner, folded])
        self.assertEqual(sum(line.count("● GPU") for line in text), 1)
        self.assertIn("미등록", next(line for line in text if "● GPU" in line))

    def test_projection_cost_is_bounded(self):
        hosts = []
        for h in range(3):
            hosts.append({"host": "h%d" % h, "self": h == 0, "reachable": True, "gpus": [
                {"index": g, "name": "NVIDIA A100", "processes": [
                    _process(pid=1000 + h * 1000 + g * 20 + p, proc_start=p,
                             cwd="/data/proj%d" % (p % 5))
                    for p in range(20)]}
                for g in range(8)]})
        snapshot = {"configured": True, "observed_at": time.time(), "hosts": hosts}

        def build():
            started = time.perf_counter()
            built = render._build_lines([], [], "both", False, 0, term_width=120)
            return time.perf_counter() - started, built

        render.set_compute_hosts({"configured": True, "observed_at": time.time(), "hosts": []})
        baseline, _ = build()
        render.set_compute_hosts(snapshot)
        with_gpu, built = build()
        text = [render._plain(line) for line in built if line is not None]
        self.assertEqual(sum(line.count("● GPU") for line in text), 480)
        self.assertEqual(len(compute_hosts.unregistered_gpu(snapshot)), 480)
        # Generous ceiling for slow CI; the plan's target is ~50 ms of added work.
        self.assertLess(with_gpu - baseline, 0.5)


class UnregisteredGpuJsonTest(unittest.TestCase):
    def test_json_adds_unregistered_gpu_additively(self):
        snapshot = _snapshot((0, [_process(pid=500, pgid=490), _process(pid=600, pgid=590)]))
        registered = ResourceJob(run_id="r1", liveness="working", pid=488, starttime="1",
                                 process_group=490)
        with mock.patch.object(fleet, "_collect_memory", return_value=None), \
             mock.patch.object(fleet, "_collect_governor", return_value=None):
            payload = json.loads(fleet._snapshot_json(
                [], [], resource_jobs=[registered], compute_host_snapshot=snapshot))
            bare = json.loads(fleet._snapshot_json([], []))
        self.assertEqual(payload["compute_hosts"]["hosts"], snapshot["hosts"])
        self.assertNotIn("unregistered_gpu", bare)
        (entry,) = payload["unregistered_gpu"]
        self.assertEqual(entry["pid"], 600)
        self.assertEqual(entry["project"], "SR_CorrNet_DSC")
        self.assertEqual(entry["used_memory_mib"], 19208)
        self.assertEqual(entry["gpu_indexes"], [0])
        self.assertEqual(entry["cwd"], SR_CWD)
        self.assertTrue(entry["self"])


if __name__ == "__main__":
    unittest.main()
