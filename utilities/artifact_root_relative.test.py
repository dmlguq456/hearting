#!/usr/bin/env python3
"""An artifact root typed as a relative path answers exactly as the absolute one."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import unittest

os.environ.setdefault("AGENT_ARTIFACT_CHECKPOINT", "off")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import artifact_campaign as C
import artifact_producer as P

_spec = importlib.util.spec_from_file_location("root_relative_producer_fixture", HERE / "artifact_producer.test.py")
F = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(F)


FROZEN = """\
import itertools, os, sys, time
sys.path.insert(0, sys.argv[1])
_ticks = itertools.count(1)
time.time = lambda: 1760000000.0
os.urandom = lambda n: (next(_ticks) % (256 ** n)).to_bytes(n, "big")
import datetime as _dt
class _Fixed(_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 19, 12, 0, 0, tzinfo=tz)
import artifact_campaign, artifact_producer
artifact_campaign.datetime = _Fixed
sys.exit(artifact_producer.main(sys.argv[2:]))
"""


def tree(base):
    """Every path under base with its bytes (a symlink by its target)."""
    out = {}
    for path in sorted(Path(base).rglob("*")):
        key = path.relative_to(base).as_posix()
        if path.is_symlink():
            out[key] = "->" + os.readlink(path)
        elif path.is_file():
            out[key] = path.read_bytes().hex()
        else:
            out[key] = "dir"
    return out


# Caches that carry file-system inode numbers and history lines whose ids hash the real clock: they differ
# between any two runs of the same command, however the root was typed.
RUN_SPECIFIC = (".runtime/artifact-admission/v1/index.json.head.json", ".runtime/routes/.route-children-index.json",
                ".runtime/artifact-producer/v1/cycles/.cycle-routes-index.json",
                ".runtime/campaign-document/watermark.json")  # the campaign documents' pass mark is a clock reading
HISTORY = ".runtime/artifact-producer/v1/history/"


def comparable(snapshot, root_name):
    kept = {k: v for k, v in snapshot.items()
            if not k.startswith(root_name + "/" + HISTORY) and k[len(root_name) + 1:] not in RUN_SPECIFIC}
    history = sorted(k for k in snapshot if k.startswith(root_name + "/" + HISTORY) and k.endswith(".jsonl"))
    return kept, len(history)


class RelativeRootBase(F.ProducerTestBase):
    stale = False

    def setUp(self):
        super().setUp()
        self.parent = self.root.parent
        self.stub = self.parent / "frozen_run.py"
        self.stub.write_text(FROZEN)
        self.open = self._begin("open-one", "open-stream")
        self.closed = self._begin("closed-one", "closed-stream", seal=True)
        self.closed_json = Path(self.closed["cycle_dir"]).parent / "campaign.json"
        C.close(self.root, self.closed_json, reason="done")
        self.ready = self._begin("ready-one", "ready-stream", seal=True)
        self.spare, _ = self.route(slug="spare-one", gate_source="spare-one")
        if self.stale:
            # The closed campaign's record lags its events: a projection the writers repair.
            record = json.loads(self.closed_json.read_text())
            for key in ("satisfied_on", "satisfaction_event_id"):
                record.pop(key, None)
            record["state"] = "active"
            self.closed_json.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
            self.assertTrue(C.campaign_state(self.root, self.closed_json).projection_pending)
        self.wait_for_entrance()
        self.backup = self.parent.parent / (self.parent.name + "-backup")
        shutil.copytree(self.parent, self.backup, symlinks=True)
        self.addCleanup(shutil.rmtree, self.backup, ignore_errors=True)

    def _begin(self, slug, key, seal=False):
        route, route_file = self.route(slug=slug, gate_source=slug)
        result = P.begin(self.root, route_file=route_file, capability="autopilot-code",
                         intensity="direct", campaign_key=key)
        if seal:
            self.write_output(result)
            self.close(route, route_file)
            P.finalize(self.root, cycle_id=result["cycle_id"])
        return result

    def restore(self):
        for child in self.parent.iterdir():
            shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
        for child in self.backup.iterdir():
            (shutil.copytree(child, self.parent / child.name, symlinks=True) if child.is_dir()
             else shutil.copy2(child, self.parent / child.name))

    def spellings(self):
        """(cwd, root as typed): beside the root, inside it, and two levels down."""
        deep = self.closed_json.parent
        deep_rel = os.path.relpath(self.root, deep)
        return {"beside": (self.parent, self.root.name), "inside": (self.root, "."), "deep": (deep, deep_rel)}

    def run_cli(self, cwd, root_arg, argv):
        proc = subprocess.run([sys.executable, str(self.stub), str(HERE), argv[0], "--artifact-root", root_arg, *argv[1:]],
                              cwd=str(cwd), env=dict(os.environ), capture_output=True, text=True, timeout=180)
        self.wait_for_entrance()  # the command's own detached entrance refresh settles before the disk is read
        try:
            out = json.loads(proc.stdout)
        except ValueError:
            out = proc.stdout
        return proc.returncode, out, proc.stderr

    def run_and_snapshot(self, cwd, root_arg, argv):
        self.restore()
        rc, out, err = self.run_cli(cwd, root_arg, argv)
        return (rc, out, err), tree(self.parent)

    def assert_same_answer(self, argv, label, *, names=("beside", "inside", "deep"), disk=True):
        """The command typed with an absolute root and with each relative spelling answers alike."""
        want, want_disk = self.run_and_snapshot(self.parent, str(self.root), argv)
        for name in names:
            cwd, typed = self.spellings()[name]
            got, got_disk = self.run_and_snapshot(cwd, typed, argv)
            with self.subTest(command=label, spelling=name):
                self.assertEqual(got, want)
                if disk:
                    self.assertEqual(comparable(got_disk, self.root.name), comparable(want_disk, self.root.name))
                    # nothing was made beside the root, nor under a doubled root
                    outside = lambda snap: sorted(k for k in snap if k.split("/")[0] != self.root.name)  # noqa: E731
                    self.assertEqual(outside(got_disk), outside(want_disk))
                    self.assertFalse([k for k in got_disk if k.startswith(self.root.name + "/" + self.root.name)])
        return want


class RelativeRootTest(RelativeRootBase):
    def test_reads_answer_alike_and_a_closed_campaign_stays_satisfied(self):
        closed, active = self.closed["campaign_id"], self.open["campaign_id"]
        listing = self.assert_same_answer(("campaign-list", "--all-states"), "campaign-list", disk=False)[1]
        states = {row["key"]: row["state"] for row in listing["campaigns"]}
        self.assertEqual(states, {"open-stream": "active", "closed-stream": "satisfied", "ready-stream": "active"})
        self.assert_same_answer(("campaign-list",), "campaign-list active", disk=False)
        self.assert_same_answer(("campaign-status", "--campaign", closed), "campaign-status closed", disk=False)
        self.assert_same_answer(("campaign-status", "--campaign", active), "campaign-status open", disk=False)
        self.assert_same_answer(("status",), "status", disk=False)
        self.assert_same_answer(("campaign-export",), "campaign-export", disk=False)
        self.assert_same_answer(("cycle-move", "--cycle", self.ready["cycle_id"], "--campaign", active, "--dry-run"),
                                "cycle-move --dry-run", disk=False)

    def test_reopening_a_closed_campaign_leaves_the_same_bytes(self):
        self.assert_same_answer(("campaign-reopen", "--campaign", self.closed["campaign_id"], "--reason", "again"),
                                "campaign-reopen")
        # byte for byte: only run-specific caches and clock-hashed history are set aside above, and this
        # command writes none of them
        want, want_disk = self.run_and_snapshot(self.parent, str(self.root), (
            "campaign-reopen", "--campaign", self.closed["campaign_id"], "--reason", "again"))
        got, got_disk = self.run_and_snapshot(self.parent, self.root.name, (
            "campaign-reopen", "--campaign", self.closed["campaign_id"], "--reason", "again"))
        self.assertEqual(got_disk, want_disk)

    def test_writes_leave_the_same_disk(self):
        closed, ready, opened = self.closed["campaign_id"], self.ready["campaign_id"], self.open["campaign_id"]
        for label, argv in {
            "campaign-close": ("campaign-close", "--campaign", ready, "--reason", "done"),
            "campaign-recover": ("campaign-recover", "--campaign", closed),
            "recover": ("recover",),
            "cycle-mark": ("cycle-mark", "--cycle", self.closed["cycle_id"], "--discard", "--reason", "x"),
            "cycle-move into closed": ("cycle-move", "--cycle", self.ready["cycle_id"], "--campaign", closed),
            "cycle-move": ("cycle-move", "--cycle", self.ready["cycle_id"], "--campaign", opened),
            "delete cycle": ("delete", "--cycle", self.closed["cycle_id"], "--reason", "x"),
            "delete campaign": ("delete", "--campaign", closed, "--reason", "x"),
            "finalize": ("finalize", "--cycle", self.open["cycle_id"], "--state", "abandoned",
                         "--abandon-reason", sorted(P.ABANDON_REASONS)[0]),
            "begin into closed key": ("begin", "--route", self.spare["route_id"], "--capability", "autopilot-code",
                                      "--intensity", "direct", "--campaign-key", "closed-stream"),
        }.items():
            self.assert_same_answer(argv, label, names=("beside", "deep"))

    def test_a_root_reached_through_a_link_answers_as_the_absolute_spelling_of_that_link(self):
        link = self.parent / "link"
        link.symlink_to(self.root.name)
        for argv in (("campaign-list", "--all-states"), ("status",), ("campaign-export",)):
            want = self.run_cli(self.parent, str(link), argv)
            got = self.run_cli(self.parent, "link", argv)
            with self.subTest(command=argv[0]):
                self.assertEqual(got, want)


class RelativeRootStaleProjectionTest(RelativeRootBase):
    stale = True

    def test_repair_and_reads_of_a_lagging_record_answer_alike(self):
        closed = self.closed["campaign_id"]
        self.assert_same_answer(("campaign-list", "--all-states"), "campaign-list", names=("beside", "inside"), disk=False)
        self.assert_same_answer(("campaign-status", "--campaign", closed), "campaign-status", names=("beside",), disk=False)
        self.assert_same_answer(("campaign-recover", "--campaign", closed), "campaign-recover", names=("beside", "deep"))
        self.assert_same_answer(("recover",), "recover", names=("beside", "deep"))


class RelativeRootLibraryTest(RelativeRootBase):
    """Callers that reach the fold without the command line: the root they hold may be relative too."""

    def setUp(self):
        super().setUp()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.parent)

    def test_listing_and_fold_read_a_relative_root_as_the_absolute_one(self):
        relative = Path(self.root.name)
        self.assertEqual(P.list_campaign_summaries(relative, active_only=False),
                         P.list_campaign_summaries(self.root, active_only=False))
        self.assertEqual(P.list_campaign_summaries(relative, active_only=False, include_dispositions=False),
                         P.list_campaign_summaries(self.root, active_only=False, include_dispositions=False))
        for path in (self.closed_json, Path(self.ready["cycle_dir"]).parent / "campaign.json",
                     Path(self.open["cycle_dir"]).parent / "campaign.json"):
            self.assertEqual(C.campaign_state(relative, path.relative_to(self.parent)),
                             C.campaign_state(self.root, path))
        self.assertEqual(C.campaign_state(relative, self.closed_json.relative_to(self.parent)).state, "satisfied")


if __name__ == "__main__":
    unittest.main()
