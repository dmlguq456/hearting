"""Live number allocation, fixed Claude reservations and consumer agreement."""
import importlib.util
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))
from fleet import herdr_projection, session_tags
from fleet.model import Session
from fleet.session_handle import minted_tag, resolve_tag
from fleet.collectors import procscan
REPORT = herdr_projection._report


def collision():
    seen = {}
    for n in range(300):
        sid = "collision-%d" % n
        tag = minted_tag(sid)
        if tag in seen:
            return seen[tag], sid
        seen[tag] = sid
    raise AssertionError("no collision")


class SessionTagCollisionTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root),
                                              "FLEET_SESSION_REGISTRY_DIR": str(self.root / "registry"),
                                              "CLAUDE_CONFIG_DIR": str(self.root / "claude")})
        self.env.start()
        self.addCleanup(self.env.stop)
        patch = mock.patch("fleet.collectors.herdr.list_agents", return_value=[])
        self.agents = patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch("fleet.herdr_projection._report")
        self.report = patch.start()
        self.addCleanup(patch.stop)
        self.a, self.b = collision()

    def session(self, sid, harness="codex", started=10, tag=None):
        return Session(harness=harness, session_id=sid, started_at=started,
                       session_tag=tag, pid=os.getpid(), proc_start=procscan.read_proc_start(os.getpid()))

    def tags(self, sessions):
        session_tags.refresh(sessions)
        return [resolve_tag(s.harness, s.session_id) for s in sessions]

    def test_bootstrap_orders_existing_pair_by_start_not_collection_order(self):
        older, newer = self.session(self.a), self.session(self.b, started=20)
        new_tag, old_tag = self.tags([newer, older])
        self.assertEqual(old_tag, minted_tag(self.a))
        self.assertNotEqual(new_tag, old_tag)
        self.assertEqual(self.tags([older, newer]), [old_tag, new_tag])

    def test_first_assignment_keeps_number_when_earlier_session_is_seen_later(self):
        first = self.session(self.b, started=20)
        first_tag = self.tags([first])[0]
        newcomer = self.session(self.a, started=10)
        self.assertEqual(self.tags([newcomer, first])[1], first_tag)
        self.assertNotEqual(resolve_tag("codex", self.a), first_tag)

    def test_cross_harness_collision_and_restart_consumers_agree(self):
        sessions = [self.session(self.a), self.session(self.b, "opencode", started=20)]
        tags = self.tags(sessions)
        self.assertEqual(len(set(tags)), 2)
        script = "from fleet.session_handle import resolve_tag; import sys; print(resolve_tag(sys.argv[1], sys.argv[2]))"
        for sess, tag in zip(sessions, tags):
            env = dict(os.environ, PYTHONPATH=str(ROOT / "tools"))
            actual = subprocess.check_output([sys.executable, "-c", script, sess.harness, sess.session_id], env=env, text=True).strip()
            self.assertEqual(actual, tag)
            agent, _ = herdr_projection.compose(sess.harness, sess.session_id, steward=False, title="")
            self.assertEqual(agent, "[%s] %s" % (tag, sess.harness))
        spec = importlib.util.spec_from_file_location("peer_message_tags", ROOT / "utilities/peer-message.py")
        peer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(peer)
        self.assertEqual(peer.peer_alias("opencode", self.b), "[%s]" % tags[1])

    def test_claude_number_is_fixed_and_codex_moves_even_if_already_assigned(self):
        cx = self.session(self.a)
        original = self.tags([cx])[0]
        cl = self.session("fixed-claude", "claude", started=30, tag=original)
        self.tags([cx, cl])
        self.assertNotEqual(resolve_tag("codex", self.a), original)
        rows = session_tags._read(session_tags._path())
        self.assertEqual(rows[("claude", "fixed-claude")]["tag"], original)

    def test_displaced_session_does_not_steal_another_existing_number(self):
        first, second = self.session(self.a), self.session(self.b, started=20)
        a_tag, b_tag = self.tags([first, second])
        self.tags([self.session("cl", "claude", tag=a_tag), first, second])
        self.assertEqual(resolve_tag("codex", self.b), b_tag)
        self.assertNotIn(resolve_tag("codex", self.a), {a_tag, b_tag})

    def test_record_loss_and_corruption_leave_number_absent(self):
        self.tags([self.session(self.a), self.session(self.b, started=20)])
        path = session_tags._path()
        path.unlink()
        self.assertIsNone(resolve_tag("codex", self.b))
        path.write_text("{broken")
        self.assertIsNone(resolve_tag("codex", self.b))
        self.tags([self.session(self.a), self.session(self.b)])
        self.assertEqual(path.read_text(), "{broken")

    def test_duplicate_mutable_assignments_are_corrupt_and_numberless(self):
        self.tags([self.session(self.a), self.session(self.b, started=20)])
        data = json.loads(session_tags._path().read_text())
        for row in data["assignments"]:
            row["tag"] = "00"
        session_tags._path().write_text(json.dumps(data))
        self.assertIsNone(resolve_tag("codex", self.a))
        self.assertIsNone(resolve_tag("codex", self.b))

    def test_unwritable_state_does_not_break_consumer(self):
        with mock.patch("fleet.session_tags.tempfile.mkstemp", side_effect=PermissionError):
            self.tags([self.session(self.a), self.session(self.b)])
        self.assertIsNone(resolve_tag("codex", self.b))

    def test_live_process_survives_partial_collection_and_pid_reuse_releases(self):
        self.tags([self.session(self.a)])
        self.tags([])
        self.assertEqual(len(session_tags._read(session_tags._path())), 1)
        with mock.patch("fleet.collectors.procscan.read_proc_start", return_value="reused"):
            session_tags.refresh([])
        self.assertEqual(session_tags._read(session_tags._path()), {})

    def test_unreadable_live_process_identity_keeps_its_reservation(self):
        confirmed = self.tags([self.session(self.a)])[0]
        with mock.patch("fleet.collectors.procscan.read_proc_start", return_value=None), \
             mock.patch("fleet.collectors.procscan.is_terminal_state", return_value=False):
            self.tags([])
        self.assertEqual(resolve_tag("codex", self.a), confirmed)

    def test_zombie_process_releases_its_number(self):
        self.tags([self.session(self.a)])
        with mock.patch("fleet.collectors.procscan.read_proc_start", return_value=None), \
                mock.patch("fleet.collectors.procscan.is_terminal_state", return_value=True):
            session_tags.refresh([])
        self.assertEqual(session_tags._read(session_tags._path()), {})

    def test_herdr_reserves_fixed_claude_outside_filtered_fleet_collection(self):
        (self.root / "claude/sessions").mkdir(parents=True)
        (self.root / "claude/sessions/1.json").write_text(json.dumps({"sessionId": "cl", "nameSource": "derived", "name": "hearting-" + minted_tag(self.a)}))
        self.agents.return_value = [{"agent": "claude", "pane_id": "w:p", "agent_session": {"kind": "id", "value": "cl"}}]
        self.assertNotEqual(self.tags([self.session(self.a)])[0], minted_tag(self.a))

    def test_first_consumers_are_read_only_until_observed_producer(self):
        self.agents.return_value = [{"agent": harness, "pane_id": "w:" + sid,
                                    "agent_session": {"kind": "id", "value": sid}}
                                   for harness, sid in (("codex", self.a), ("opencode", self.b))]
        with mock.patch.object(session_tags, "assign", side_effect=AssertionError("reader allocated")):
            self.assertIsNone(resolve_tag("codex", self.a))
            self.assertIsNone(resolve_tag("opencode", self.b))
            self.assertFalse(session_tags._path().parent.exists())
        self.agents.assert_not_called()
        first = session_tags.assign_observed("codex", self.a, pane_id="w:a")
        second = session_tags.assign_observed("opencode", self.b, pane_id="w:b")
        self.agents.assert_not_called()
        self.assertNotEqual(first, second)
        self.assertEqual(resolve_tag("codex", self.a), first)

    def test_new_live_session_resolves_before_next_fleet_tick(self):
        before = self.tags([self.session(self.a)])[0]
        self.agents.return_value = [{"agent": "codex", "pane_id": "w:new",
                                    "agent_session": {"kind": "id", "value": self.b}}]
        self.assertIsNone(resolve_tag("codex", self.b))
        self.assertNotEqual(session_tags.assign_observed("codex", self.b, pane_id="w:new"), before)

    def test_sequential_native_first_reports_allocate_before_metadata(self):
        agents, reported = [], []
        self.agents.side_effect = AssertionError("native producer started a census")

        def run(command, **kwargs):
            if command[2] == "report-agent-session":
                sid = command[command.index("--agent-session-id") + 1]
                agents.append({"agent": command[command.index("--agent") + 1], "pane_id": command[3],
                               "agent_session": {"kind": "id", "value": sid}})
            elif command[2] == "report-metadata":
                shown = command[command.index("--display-agent") + 1]
                reported.append(shown)
                next(a for a in agents if a["pane_id"] == command[3])["display_agent"] = shown
            return SimpleNamespace(returncode=0)

        with mock.patch("fleet.herdr_projection.subprocess.run", side_effect=run), \
                mock.patch("fleet.herdr_projection.shutil.which", return_value="herdr"), \
                mock.patch("fleet.herdr_projection.session_title", return_value=""), \
                mock.patch("fleet.herdr_projection._formatter_overrides", return_value=(None, None)):
            for harness, sid in (("codex", self.a), ("opencode", self.b)):
                REPORT(harness, sid, "w:" + sid, True, session_seq=1, session_start_source="startup")
        self.assertEqual(len(reported), 2)
        self.assertNotEqual(reported[0], reported[1])
        self.assertEqual(reported, ["[%s] %s" % (resolve_tag(harness, sid), harness)
                                    for harness, sid in (("codex", self.a), ("opencode", self.b))])

    def test_idle_colliding_herdr_pane_updates_metadata_without_identity_or_prompt(self):
        self.agents.return_value = [{"agent": "codex", "pane_id": "w:" + sid,
                                    "display_agent": "[%s] codex" % minted_tag(sid),
                                    "agent_session": {"kind": "id", "value": sid}}
                                   for sid in (self.a, self.b)]
        self.tags([self.session(self.a), self.session(self.b, started=20)])
        self.report.assert_called_once_with("codex", self.b, "w:" + self.b, False)
        self.agents.return_value[1]["display_agent"] = "[%s] codex" % resolve_tag("codex", self.b)
        self.tags([])
        self.assertEqual(self.report.call_count, 1)

    def test_non_herdr_registry_reserves_claude(self):
        from fleet import session_registry
        directory = self.root / "claude/sessions"
        directory.mkdir(parents=True)
        pid = os.getpid()
        (directory / (str(pid) + ".json")).write_text(json.dumps({"sessionId": "outside-cl", "procStart": procscan.read_proc_start(pid),
                                                               "nameSource": "derived", "name": "hearting-" + minted_tag(self.a)}))
        self.assertNotEqual(self.tags([self.session(self.a)])[0], minted_tag(self.a))

    def test_codex_registry_without_process_start_does_not_reserve_recycled_sid(self):
        directory = self.root / "registry/codex"
        directory.mkdir(parents=True)
        (directory / (str(os.getpid()) + ".json")).write_text(json.dumps({"sessionId": self.a}))
        session_tags.refresh([])
        self.assertNotIn(("codex", self.a), session_tags._read(session_tags._path()))

    def test_unknown_identity_does_not_gain_a_number(self):
        self.tags([Session(harness="codex", pid=os.getpid())])
        self.assertIsNone(resolve_tag("codex", ""))
        self.assertEqual(session_tags._read(session_tags._path()), {})

    def test_empty_first_inventory_recovers_on_next_normal_collection(self):
        self.assertIsNone(resolve_tag("codex", self.a))
        session_tags.assign([])
        self.agents.return_value = [{"agent": "codex", "pane_id": "w:" + sid,
                                    "agent_session": {"kind": "id", "value": sid}}
                                   for sid in (self.a, self.b)]
        self.assertIsNone(resolve_tag("codex", self.b))
        session_tags.assign([])
        self.assertNotEqual(resolve_tag("codex", self.a), resolve_tag("codex", self.b))

    def test_herdr_capacity_exhaustion_is_numberless_without_recursive_refresh(self):
        self.agents.return_value = [{"agent": "codex", "pane_id": "w:" + str(n),
                                    "agent_session": {"kind": "id", "value": "live-%d" % n}}
                                   for n in range(257)]
        session_tags.refresh([])
        rows = session_tags._read(session_tags._path())
        self.assertEqual(len(rows), 256)
        missing = next("live-%d" % n for n in range(257) if ("codex", "live-%d" % n) not in rows)
        self.assertIsNone(resolve_tag("codex", missing))

    def test_uuid_creation_order_for_pre_install_herdr_pair(self):
        older = "01a1223d-621b-7908-8399-1f234f623c79"
        newer = "01a1223f-5bfb-7dca-b092-a1db86a2d707"
        self.assertLess(session_tags._started("codex", older), session_tags._started("codex", newer))

    def test_capacity_exhaustion_keeps_existing_256_assignments(self):
        sessions = [self.session("session-%d" % n, started=n) for n in range(256)]
        before = self.tags(sessions)
        self.assertEqual(len(set(before)), 256)
        self.tags(sessions + [self.session("extra", started=300)])
        self.assertEqual([resolve_tag(s.harness, s.session_id) for s in sessions], before)
        self.assertIsNone(resolve_tag("codex", "extra"))

    def test_failures_never_display_an_unconfirmed_hash_for_either_harness(self):
        for harness in ("codex", "opencode"):
            with self.subTest(harness=harness):
                path = session_tags._path()
                path.unlink(missing_ok=True)
                self.assertIsNone(resolve_tag(harness, self.b))
                session_tags.assign_observed(harness, self.a)
                confirmed = session_tags.assign_observed(harness, self.b)
                self.assertNotEqual(confirmed, minted_tag(self.b))
                self.assertEqual(resolve_tag(harness, self.b), confirmed)
                for failure in (PermissionError(), ValueError()):
                    with mock.patch.object(session_tags, "_read", side_effect=failure):
                        self.assertIsNone(resolve_tag(harness, self.b))
                with mock.patch.dict(sys.modules, {"fleet.session_tags": None}):
                    self.assertIsNone(resolve_tag(harness, self.b))
                path.unlink()
                self.assertIsNone(resolve_tag(harness, self.b))
                path.write_text("{broken")
                self.assertIsNone(resolve_tag(harness, self.b))
                session_tags.assign_observed(harness, self.b)
                self.assertEqual(path.read_text(), "{broken")

    def test_real_writer_lock_readers_and_producers_return_without_waiting(self):
        sessions = [self.session(self.a), self.session(self.b, started=20)]
        confirmed = self.tags(sessions)[1]
        path = session_tags._path()
        snapshot = path.read_bytes()
        # The child reads and attempts production against a genuinely held lock.
        script = """
import json
from unittest import mock
from fleet import session_tags
from fleet.session_handle import resolve_tag
with mock.patch.object(session_tags, '_inventory', side_effect=AssertionError('reader census')):
    values = [resolve_tag('codex', %r), resolve_tag('codex', 'missing')]
    assert session_tags.assign_observed('codex', 'missing') is None
with mock.patch.object(session_tags, '_inventory', return_value=({}, [])):
    assert session_tags.assign([]) == []
print(json.dumps(values))
""" % self.b
        with path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            proc = subprocess.run([sys.executable, "-c", script], timeout=3, capture_output=True,
                                  text=True, env=dict(os.environ, PYTHONPATH=str(ROOT / "tools")))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout), [confirmed, None])
            self.assertEqual(path.read_bytes(), snapshot)
        # A leftover lock file is not a lock; the next normal event fills the miss.
        self.assertIsNone(resolve_tag("codex", "missing"))
        self.assertIsNotNone(session_tags.assign_observed("codex", "missing"))
        self.assertEqual(resolve_tag("codex", self.b), confirmed)

    def test_narrow_observation_preserves_other_rows_and_absent_evidence(self):
        self.tags([self.session(self.a), self.session(self.b, started=20)])
        before = session_tags._read(session_tags._path())
        with mock.patch.object(session_tags, "_inventory", side_effect=AssertionError("census")), \
             mock.patch.object(session_tags, "_keep", side_effect=AssertionError("absence")):
            session_tags.assign_observed("opencode", "new", pane_id="w:new")
            session_tags.assign_observed("codex", self.b, pane_id="w:moved")
        after = session_tags._read(session_tags._path())
        self.assertEqual(before[("codex", self.a)], after[("codex", self.a)])
        expected = dict(before[("codex", self.b)], pane_id="w:moved")
        self.assertEqual({k: v for k, v in expected.items() if k != "observation_revision"},
                         {k: v for k, v in after[("codex", self.b)].items()
                          if k != "observation_revision"})

    def test_collection_discovers_outside_lock_then_rereads_latest_snapshot(self):
        confirmed = []

        def inventory(sessions):
            # Would contend/fail if inventory ran while the writer held its lock.
            confirmed.append(session_tags.assign_observed("opencode", self.b, pane_id="w:new-live"))
            self.assertIsNotNone(confirmed[0])
            return {("codex", self.a): {"harness": "codex", "session_id": self.a,
                                        "started_at": 10}}, []
        with mock.patch.object(session_tags, "_inventory", side_effect=inventory):
            session_tags.assign([])
        first, second = resolve_tag("codex", self.a), resolve_tag("opencode", self.b)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first, second)
        self.assertEqual(second, confirmed[0])

    def test_stale_collection_keeps_same_sid_new_pane_and_process_evidence(self):
        key = ("codex", self.a)
        confirmed = session_tags.assign_observed(*key, pane_id="w:old", pid=101, proc_start="10")
        stale = session_tags._read(session_tags._path())[key].copy()
        latest = {}

        def inventory(sessions):
            self.assertEqual(session_tags.assign_observed(*key, pane_id="w:new", pid=202,
                                                         proc_start="20"), confirmed)
            latest.update(session_tags._read(session_tags._path()))
            return {key: stale}, []

        with mock.patch.object(session_tags, "_inventory", side_effect=inventory):
            session_tags.assign([])
        self.assertEqual(session_tags._read(session_tags._path()), latest)
        self.assertEqual(resolve_tag(*key), confirmed)

    def test_identical_native_and_full_reaffirmations_survive_a_stale_census(self):
        inventory = session_tags._inventory
        for harness in ("codex", "opencode"):
            for event in ("native", "full"):
                for legacy in (False, True):
                    with self.subTest(harness=harness, event=event, legacy=legacy):
                        session_tags._path().unlink(missing_ok=True)
                        key = (harness, self.a)
                        original = session_tags.assign_observed(*key, pane_id="w:same")
                        if legacy:
                            data = json.loads(session_tags._path().read_text())
                            data["assignments"][0].pop("observation_revision", None)
                            session_tags._path().write_text(json.dumps(data))
                            self.assertEqual(resolve_tag(*key), original)
                        agents = [{"agent": harness, "pane_id": "w:same",
                                   "agent_session": {"kind": "id", "value": self.a}}]

                        def stale_inventory(_):
                            if event == "native":
                                herdr_projection._assign_observed_tag(*key, "w:same")
                            else:
                                self.agents.return_value = agents
                                with mock.patch.object(session_tags, "_inventory", wraps=inventory):
                                    self.assertEqual(session_tags.assign([]), agents)
                            self.assertEqual(resolve_tag(*key), original)
                            return {}, []

                        with mock.patch.object(session_tags, "_inventory", side_effect=stale_inventory):
                            session_tags.assign([])
                        self.assertEqual(resolve_tag(*key), original)
                        other = "opencode" if harness == "codex" else "codex"
                        allocated = session_tags.assign_observed(other, self.b, pane_id="w:other")
                        self.assertIsNotNone(allocated)
                        self.assertNotEqual(allocated, original)
                        self.assertEqual(session_tags.assign_observed(*key, pane_id="w:same"), original)

    def test_identical_reaffirmation_protects_pid_and_pane_from_stale_live_evidence(self):
        for harness in ("codex", "opencode"):
            with self.subTest(harness=harness):
                session_tags._path().unlink(missing_ok=True)
                key = (harness, self.a)
                original = session_tags.assign_observed(*key, pane_id="w:same", pid=101, proc_start="10")
                row = session_tags._read(session_tags._path())[key].copy()

                def stale_inventory(_):
                    self.assertEqual(session_tags.assign_observed(*key, pane_id="w:same",
                                                                pid=101, proc_start="10"), original)
                    return {key: dict(row, pane_id="w:stale", pid=202, proc_start="20")}, []

                with mock.patch.object(session_tags, "_inventory", side_effect=stale_inventory):
                    session_tags.assign([])
                actual = session_tags._read(session_tags._path())[key]
                self.assertEqual((actual["pane_id"], actual["pid"], actual["proc_start"]),
                                 ("w:same", 101, "10"))
                self.assertEqual(actual["tag"], original)

    def test_carrying_another_reservation_does_not_reaffirm_it(self):
        for event in ("native", "full"):
            with self.subTest(event=event):
                session_tags._path().unlink(missing_ok=True)
                key, other = ("codex", self.a), ("opencode", self.b)
                session_tags.assign_observed(*key, pane_id="w:gone")

                def stale_inventory(_):
                    if event == "native":
                        session_tags.assign_observed(*other, pane_id="w:other")
                    else:
                        live = {other: {"harness": other[0], "session_id": other[1],
                                        "started_at": 20, "pane_id": "w:other"}}
                        with mock.patch.object(session_tags, "_inventory", return_value=(live, None)):
                            session_tags.assign([])
                    return {}, []

                with mock.patch.object(session_tags, "_inventory", side_effect=stale_inventory):
                    session_tags.assign([])
                self.assertIsNone(resolve_tag(*key))
                self.assertIsNotNone(resolve_tag(*other))

    def test_overlapping_full_collections_preserve_newer_rows_and_evidence(self):
        key, other = ("codex", self.a), ("opencode", self.b)
        confirmed = session_tags.assign_observed(*key, pane_id="w:old")
        stale = session_tags._read(session_tags._path())[key].copy()
        latest = {}

        def inventory(sessions):
            newer = {key: dict(stale, pane_id="w:moved", pid=202, proc_start="20"),
                     other: {"harness": "opencode", "session_id": self.b,
                             "started_at": 20, "pane_id": "w:other"}}
            with mock.patch.object(session_tags, "_inventory", return_value=(newer, [])):
                session_tags.assign([])
            latest.update(session_tags._read(session_tags._path()))
            self.assertEqual(latest[key]["pane_id"], "w:moved")
            self.assertIn(other, latest)
            return {key: stale}, []

        with mock.patch.object(session_tags, "_inventory", side_effect=inventory):
            session_tags.assign([])
        self.assertEqual(session_tags._read(session_tags._path()), latest)
        self.assertEqual(resolve_tag(*key), confirmed)
        self.assertIsNotNone(resolve_tag(*other))
        self.assertNotEqual(resolve_tag(*other), confirmed)
        # The merge protects this census only; a fresh proven exit still releases.
        with mock.patch("fleet.collectors.procscan.read_proc_start", return_value="reused"):
            session_tags.assign([])
        self.assertEqual(session_tags._read(session_tags._path()), {})

    def test_stale_collection_does_not_resurrect_a_concurrently_deleted_row(self):
        key, other = ("codex", self.a), ("opencode", self.b)
        confirmed = session_tags.assign_observed(*key, pane_id="w:gone")
        stale = session_tags._read(session_tags._path())[key].copy()
        latest = {}

        def inventory(sessions):
            newer = {other: {"harness": "opencode", "session_id": self.b,
                             "started_at": 20, "pane_id": "w:other"}}
            with mock.patch.object(session_tags, "_inventory", return_value=(newer, [])):
                session_tags.assign([])
            latest.update(session_tags._read(session_tags._path()))
            self.assertNotIn(key, latest)
            self.assertEqual(latest[other]["tag"], confirmed)
            return {key: stale}, []

        with mock.patch.object(session_tags, "_inventory", side_effect=inventory):
            session_tags.assign([])
        self.assertEqual(session_tags._read(session_tags._path()), latest)
        self.assertIsNone(resolve_tag(*key))
        self.assertEqual(resolve_tag(*other), confirmed)
        # A later verified event can legitimately reintroduce the SID.
        returned = session_tags.assign_observed(*key, pane_id="w:returned")
        self.assertIsNotNone(returned)
        self.assertNotEqual(returned, confirmed)

    def test_unknown_native_identity_keeps_existing_and_moved_pane_reservations(self):
        for harness in ("codex", "opencode"):
            for pane in ("w:original", "w:moved"):
                with self.subTest(harness=harness, pane=pane):
                    session_tags._path().unlink(missing_ok=True)
                    confirmed = session_tags.assign_observed(harness, self.a, pane_id="w:original")
                    before = session_tags._read(session_tags._path())
                    self.agents.return_value = [{"agent": harness, "pane_id": pane,
                                                 "agent_session": {"kind": "unknown"}}]
                    self.assertEqual(session_tags.assign([]), [])
                    self.assertEqual(session_tags._read(session_tags._path()), before)
                    other = session_tags.assign_observed(harness, self.b, pane_id="w:other")
                    self.assertIsNotNone(confirmed)
                    self.assertIsNotNone(other)
                    self.assertNotEqual(confirmed, other)
                    self.agents.return_value.append({"agent": harness, "pane_id": "w:other",
                                                     "agent_session": {"kind": "id", "value": self.b}})
                    session_tags.assign([])
                    self.assertEqual(resolve_tag(harness, self.a), confirmed)
                    self.agents.return_value[0]["agent_session"] = {"kind": "id", "value": self.a}
                    session_tags.assign([])
                    row = session_tags._read(session_tags._path())[(harness, self.a)]
                    expected = dict(before[(harness, self.a)], pane_id=pane)
                    self.assertEqual({k: v for k, v in row.items() if k != "observation_revision"},
                                     {k: v for k, v in expected.items() if k != "observation_revision"})
                    self.assertEqual(resolve_tag(harness, self.b), other)

    def test_missing_or_replaced_pane_still_releases_its_reservation(self):
        replacement = {"agent": "codex", "pane_id": "w:original",
                       "agent_session": {"kind": "id", "value": self.b}}
        unknown = {"agent": "codex", "pane_id": "w:elsewhere", "agent_session": {"kind": "unknown"}}
        for agents in ([], [replacement], [dict(replacement, agent="opencode")],
                       [dict(unknown, agent="opencode")]):
            with self.subTest(agents=agents):
                session_tags._path().unlink(missing_ok=True)
                session_tags.assign_observed("codex", self.a, pane_id="w:original")
                self.agents.return_value = agents
                session_tags.assign([])
                self.assertIsNone(resolve_tag("codex", self.a))

    def test_reused_original_seat_does_not_discard_an_unknown_moved_session(self):
        for harness in ("codex", "opencode"):
            for replacement_harness in ("codex", "opencode"):
                for reverse in (False, True):
                    with self.subTest(harness=harness, replacement=replacement_harness, reverse=reverse):
                        session_tags._path().unlink(missing_ok=True)
                        confirmed = session_tags.assign_observed(harness, self.a, pane_id="w:original")
                        replacement = {"agent": replacement_harness, "pane_id": "w:original",
                                       "agent_session": {"kind": "id", "value": self.b}}
                        moved = {"agent": harness, "pane_id": "w:moved",
                                 "agent_session": {"kind": "unknown"}}
                        self.agents.return_value = [moved, replacement] if reverse else [replacement, moved]
                        session_tags.assign([])
                        self.assertEqual(resolve_tag(harness, self.a), confirmed)
                        other = resolve_tag(replacement_harness, self.b)
                        self.assertIsNotNone(other)
                        self.assertNotEqual(other, confirmed)
                        moved["agent_session"] = {"kind": "id", "value": self.a}
                        session_tags.assign([])
                        row = session_tags._read(session_tags._path())[(harness, self.a)]
                        self.assertEqual(row["pane_id"], "w:moved")
                        self.assertEqual(row["tag"], confirmed)
                        # A fresh census with no uncertain pane still permits cleanup.
                        self.agents.return_value = [replacement]
                        session_tags.assign([])
                        self.assertIsNone(resolve_tag(harness, self.a))

    def test_failed_census_baseline_preserves_snapshot_without_discovery(self):
        confirmed = session_tags.assign_observed("codex", self.a, pane_id="w:original")
        before = session_tags._path().read_bytes()
        for failure in (PermissionError(), ValueError("corrupt baseline")):
            with self.subTest(failure=failure), \
                 mock.patch.object(session_tags, "_read", side_effect=failure), \
                 mock.patch.object(session_tags, "_inventory") as inventory:
                self.assertEqual(session_tags.assign([]), [])
                inventory.assert_not_called()
            self.assertEqual(session_tags._path().read_bytes(), before)
            self.assertEqual(resolve_tag("codex", self.a), confirmed)

    def test_invalid_observation_revision_is_corrupt_and_preserved(self):
        session_tags.assign_observed("codex", self.a, pane_id="w:original")
        path = session_tags._path()
        data = json.loads(path.read_text())
        for invalid in (-1, True, "invalid", None):
            with self.subTest(revision=invalid):
                data["assignments"][0]["observation_revision"] = invalid
                path.write_text(json.dumps(data))
                before = path.read_bytes()
                self.assertIsNone(resolve_tag("codex", self.a))
                self.assertIsNone(session_tags.assign_observed("opencode", self.b))
                self.assertEqual(path.read_bytes(), before)

    def test_failed_atomic_write_preserves_existing_assignment_and_retires_temp(self):
        confirmed = session_tags.assign_observed("codex", self.a)
        path = session_tags._path()
        before = path.read_bytes()
        for target in ("tempfile.mkstemp", "os.fsync", "os.replace"):
            with self.subTest(target=target), \
                 mock.patch("fleet.session_tags." + target, side_effect=OSError("fixture")):
                self.assertIsNone(session_tags.assign_observed("opencode", self.b))
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(resolve_tag("codex", self.a), confirmed)
                self.assertEqual(list(path.parent.glob(".tag-assignments-*")), [])

    def test_native_owned_identity_allocates_before_metadata_without_a_census(self):
        with mock.patch.object(herdr_projection.shutil, "which", return_value="herdr"), \
             mock.patch.object(herdr_projection, "may_report", return_value=True), \
             mock.patch.object(session_tags, "_inventory", side_effect=AssertionError("census")):
            for harness, sid in (("codex", self.a), ("opencode", self.b)):
                self.assertTrue(herdr_projection.project(harness, sid, pane_id="w:" + sid,
                                                        report_session=False))
                self.assertIsNotNone(resolve_tag(harness, sid))
        self.assertNotEqual(resolve_tag("codex", self.a), resolve_tag("opencode", self.b))

    def test_display_refresh_and_refused_or_stale_native_proof_never_allocate(self):
        with mock.patch.object(session_tags, "assign_observed") as assign, \
             mock.patch.object(herdr_projection.shutil, "which", return_value="herdr"), \
             mock.patch.object(herdr_projection, "may_report", return_value=False):
            herdr_projection.compose("codex", self.a, steward=False, title="")
            herdr_projection.project("opencode", self.a, pane_id="w:a", report_session=False)
            assign.assert_not_called()
        with mock.patch.object(session_tags, "assign_observed") as assign, \
             mock.patch.object(herdr_projection.shutil, "which", return_value="herdr"), \
             mock.patch.object(herdr_projection, "_send_projection") as send:
            REPORT("codex", self.a, "w:a", True, _proof=mock.Mock(side_effect=[True, False]))
            assign.assert_not_called()
            self.assertEqual(send.call_count, 1)
        self.assertFalse(session_tags._path().exists())

    def test_native_report_remains_successful_and_numberless_under_writer_lock(self):
        path = session_tags._path()
        path.parent.mkdir(parents=True)
        commands = []
        with path.with_suffix(".lock").open("a") as lock, \
             mock.patch.object(herdr_projection.shutil, "which", return_value="herdr"), \
             mock.patch.object(herdr_projection, "_send_projection", side_effect=lambda c, o: commands.append(c)), \
             mock.patch.object(herdr_projection, "session_title", return_value=""), \
             mock.patch.object(herdr_projection, "_formatter_overrides", return_value=(None, None)), \
             mock.patch.object(session_tags, "_inventory", side_effect=AssertionError("census")):
            fcntl.flock(lock, fcntl.LOCK_EX)
            REPORT("opencode", self.a, "w:a", True)
        self.assertEqual([c[2] for c in commands], ["report-agent-session", "report-metadata"])
        self.assertEqual(commands[-1][commands[-1].index("--display-agent") + 1], "opencode")
        self.assertIsNone(resolve_tag("opencode", self.a))
        self.assertIsNotNone(session_tags.assign_observed("opencode", self.a, pane_id="w:a"))

    def test_process_concurrent_refreshes_share_the_same_assignments(self):
        script = '''
import os
from fleet.session_tags import refresh
from fleet.model import Session
from fleet.collectors import herdr, procscan
herdr.list_agents = lambda: []
refresh([Session(harness='codex', session_id=sid, pid=os.getpid(),
    proc_start=procscan.read_proc_start(os.getpid()), started_at=n)
    for n, sid in enumerate(%r)])
''' % ([self.a, self.b],)
        env = dict(os.environ, PYTHONPATH=str(ROOT / "tools"))
        processes = [subprocess.Popen([sys.executable, "-c", script], env=env) for _ in range(4)]
        try:
            for process in processes:
                self.assertEqual(process.wait(timeout=10), 0)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
        self.assertNotEqual(resolve_tag("codex", self.a), resolve_tag("codex", self.b))


if __name__ == "__main__":
    unittest.main()
