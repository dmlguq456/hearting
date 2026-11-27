#!/usr/bin/env python3
"""A cycle marker read takes one open, and the begin scan still reads each marker as before.

Every route start runs the D-126 scan over every cycle folder of the artifact root. On a
network filesystem each path lookup of a folder's `.cycle.json` is a round trip, so the
lookups per read are the cost of a start on a large root (2026-11-27: a real root of 3,352
cycle folders spent most of its 30-70 s before registration in this scan). A regular marker
is now opened once, without following a link, and checked and read on that descriptor.

What the reads answer stays what it was: absent is None, a link, folder or malformed file is
`locator-cycle-binding-invalid`, an error the path questions raised (a link into a folder this
user cannot search, a too long link target) is still raised, and an invalid marker still stops
the scan. The scan still reads a folder's marker twice, once for its ID and once more, as it
is now, when `artifact_request.members` looks for request executions; a request anchor is also
read again for each execution it holds. Only the lookups per read went down.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import artifact_locator as L  # noqa: E402
import artifact_producer_cycle_ops as OPS  # noqa: E402

spec = importlib.util.spec_from_file_location("scan_reads_fixture", Path(__file__).with_name("artifact_producer.test.py"))
FX = importlib.util.module_from_spec(spec)
spec.loader.exec_module(FX)
P = FX.P


def main_read_cycle_binding(path):
    """`read_cycle_binding` as it was before the one-open read (2026-11-27), kept as the answer key."""
    marker = Path(path) / L.CYCLE_BINDING
    if not marker.exists() and not marker.is_symlink():
        return None
    binding = L._read_json(marker)
    if (binding is None or not L._BINDING_REQUIRED <= set(binding)
            or set(binding) - L._BINDING_REQUIRED - L._BINDING_OPTIONAL):
        raise L.LocatorError("locator-cycle-binding-invalid", marker.as_posix())
    if binding.get("schema_version") != 1 or binding.get("kind") != "artifact-cycle-binding":
        raise L.LocatorError("locator-cycle-binding-invalid", marker.as_posix())
    if "started_on" in binding and not L.started_on_is_valid(binding["started_on"]):
        raise L.LocatorError("locator-cycle-binding-invalid", marker.as_posix())
    if L._CAMPAIGN_ID.fullmatch(str(binding.get("campaign_id"))) is None:
        raise L.LocatorError("locator-campaign-id-invalid", str(binding.get("campaign_id")))
    if L._CYCLE_ID.fullmatch(str(binding.get("cycle_id"))) is None:
        raise L.LocatorError("locator-cycle-id-invalid", str(binding.get("cycle_id")))
    return binding


def answer(read, folder):
    """("value", binding) or (exception type, its code or errno): what a read of `folder` gives."""
    try:
        return ("value", read(folder))
    except L.LocatorError as exc:
        return ("LocatorError", exc.code)
    except OSError as exc:
        return (type(exc).__name__, exc.errno)


INVALID = ("LocatorError", "locator-cycle-binding-invalid")


class MarkerLookups:
    """Records every path lookup of a marker (stat, lstat, open) and can change the marker around one.

    `before(kind, path)` runs just before the lookup and `after(kind, path)` just after it, so a test
    can put the marker's change between any two of a read's lookups whichever read it is."""
    SURFACES = ((os, "stat", "stat"), (os, "lstat", "lstat"), (os, "open", "open"), (io, "open", "open"))

    def __init__(self, before=None, after=None):
        self.calls, self.before, self.after, self._busy = [], before, after, False

    def _marker(self, path):
        if isinstance(path, int) or not isinstance(path, (str, bytes, os.PathLike)):
            return None
        text = os.fsdecode(path)
        return Path(text) if text.endswith("/" + L.CYCLE_BINDING) else None

    def _wrap(self, real, kind):
        def lookup(path, *args, **kwargs):
            marker = None if self._busy else self._marker(path)
            if marker is not None:
                self.calls.append((kind, marker))
                self._hook(self.before, kind, marker)
            try:
                return real(path, *args, **kwargs)
            finally:
                if marker is not None:
                    self._hook(self.after, kind, marker)
        return lookup

    def _hook(self, hook, kind, marker):
        if hook is None:
            return
        self._busy = True
        try:
            hook(kind, marker)
        finally:
            self._busy = False

    def __enter__(self):
        self._patchers = [mock.patch.object(module, name, self._wrap(getattr(module, name), kind))
                          for module, name, kind in self.SURFACES]
        for patcher in self._patchers:
            patcher.start()
        return self

    def __exit__(self, *exc):
        for patcher in reversed(self._patchers):
            patcher.stop()
        return False

    def on(self, marker):
        return [kind for kind, path in self.calls if path == marker]


def replace_marker(marker, data):
    """Swap the marker for a regular file holding `data`, atomically, as a writer would."""
    staged = marker.with_name("marker.staged")
    staged.write_bytes(data)
    os.replace(staged, marker)


def link_marker(marker, target):
    """Swap the marker for a link to `target`, atomically."""
    staged = marker.with_name("marker.staged")
    staged.symlink_to(target)
    os.replace(staged, marker)


def once(when, change, marker, other):
    """A lookup hook that changes the marker the first time `when(kind)` holds, and the kinds it acted on."""
    acted = []

    def hook(kind, path):
        if acted or not when(kind):
            return
        acted.append(kind)
        if change == "remove":
            marker.unlink()
        elif change == "link":
            link_marker(marker, other)
        else:
            replace_marker(marker, other.read_bytes())
    return hook, acted


class ScanReadsTest(FX.ProducerTestBase):
    def setUp(self):
        inherited = {key: os.environ.pop(key) for key in list(os.environ) if key.startswith("AGENT_")}
        self.addCleanup(os.environ.update, inherited)
        super().setUp()
        patcher = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(Path(self._tmp.name) / "config"),
            "XDG_STATE_HOME": str(Path(self._tmp.name) / "state"),
            "HEARTING_WORKFLOW_GROUP_REVIEW": "off",
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.activate()

    def begin_cycles(self, count, *, finish=False):
        folders = []
        for index in range(count):
            route, route_file = self.route(slug=f"scan-{index}", campaign_key="scan")
            begun = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
            if finish:
                self.write_output(begun)
                self.close(route, route_file)
                P.finalize(self.root, cycle_id=begun["cycle_id"])
            folders.append(L.resolve_path(self.root, begun["cycle_id"]))
        return folders

    def locked(self, path):
        """`path` refuses this user (no search, no read) until the test ends."""
        os.chmod(path, 0o200)
        self.addCleanup(os.chmod, path, 0o700)

    def armed_in_scan(self, folder, hook):
        """Run `hook` once, just after the scan's first read of `folder`'s marker (whatever it answered)."""
        real_read, real_scan, state = L.read_cycle_binding, OPS._scan_layout, {"scanning": False, "done": False}
        folder = folder.resolve()

        def read(path):
            try:
                return real_read(path)
            finally:
                if state["scanning"] and not state["done"] and Path(path).resolve() == folder:
                    state["done"] = True
                    hook()

        def scan(root):
            state["scanning"] = True
            try:
                return real_scan(root)
            finally:
                state["scanning"] = False

        return state, mock.patch.object(L, "read_cycle_binding", side_effect=read), \
            mock.patch.object(OPS, "_scan_layout", side_effect=scan)

    def test_scan_reads_each_marker_twice_with_one_open_each(self):
        folders = self.begin_cycles(3)
        real = L.read_cycle_binding
        reads = []

        def counting(path):
            reads.append(Path(path))
            return real(path)

        with mock.patch.object(L, "read_cycle_binding", side_effect=counting), MarkerLookups() as lookups:
            scan = OPS._scan_layout(self.root)
        self.assertTrue(scan.complete, scan.blockers)
        self.assertEqual(sorted(scan.cycles.values()), sorted(folders))
        # An ordinary cycle folder: once for its ID, once more when `members` looks at it as it is now.
        self.assertEqual({folder: reads.count(folder) for folder in folders}, {folder: 2 for folder in folders})
        self.assertEqual({folder: lookups.on(folder / L.CYCLE_BINDING) for folder in folders},
                         {folder: ["open", "open"] for folder in folders})

    def test_marker_read_takes_one_path_lookup(self):
        folder = self.begin_cycles(1)[0]
        with MarkerLookups() as lookups:
            binding = L.read_cycle_binding(folder)
        self.assertTrue(L._CYCLE_ID.fullmatch(binding["cycle_id"]), binding)
        self.assertEqual(lookups.on(folder / L.CYCLE_BINDING), ["open"])

    def test_marker_answers_are_unchanged(self):
        folder = self.begin_cycles(1)[0]
        marker = folder / L.CYCLE_BINDING
        good_bytes = marker.read_bytes()
        good = json.loads(good_bytes)
        other = folder.parent / "other-binding.json"
        other.write_text(json.dumps(dict(good, cycle_id="cyc_" + "e" * 32)), encoding="utf-8")
        self.assertEqual(answer(L.read_cycle_binding, folder), ("value", good))
        self.assertEqual(answer(L.read_cycle_binding, folder / "missing-child"), ("value", None))
        self.assertEqual(answer(L.read_cycle_binding, marker), ("value", None))  # below a file: not-a-directory
        alias = folder.parent / "alias-of-cycle"
        alias.symlink_to(folder, target_is_directory=True)  # a link on the way to the marker is followed
        self.assertEqual(answer(L.read_cycle_binding, alias), ("value", good))
        target = folder / "elsewhere.json"
        target.write_text(json.dumps(good), encoding="utf-8")
        shut = folder / "shut"
        shut.mkdir()
        (shut / "target.json").write_text(json.dumps(good), encoding="utf-8")
        refuses = os.geteuid() != 0
        shapes = ("symlink", "dangling", "directory", "not-json", "not-object", "bom", "fifo",
                  "link-into-unsearchable", "link-target-too-long", "unreadable-file")
        for shape in shapes:
            if marker.is_symlink():
                marker.unlink()
            elif marker.is_dir():
                marker.rmdir()
            elif marker.exists():
                marker.unlink()
            if shape == "symlink":
                marker.symlink_to(target)
            elif shape == "dangling":
                marker.symlink_to(folder / "nowhere.json")
            elif shape == "directory":
                marker.mkdir()
            elif shape == "not-json":
                marker.write_text("{", encoding="utf-8")
            elif shape == "not-object":
                marker.write_text("[]", encoding="utf-8")
            elif shape == "bom":
                marker.write_bytes(b"\xef\xbb\xbf" + good_bytes)
            elif shape == "fifo":
                os.mkfifo(marker)
            elif shape == "link-into-unsearchable":
                marker.symlink_to(shut / "target.json")
            elif shape == "link-target-too-long":
                marker.symlink_to("n" * 300 + ".json")
            else:
                marker.write_bytes(good_bytes)
                os.chmod(marker, 0)
            if shape == "link-into-unsearchable":
                os.chmod(shut, 0o200)
            try:
                with self.subTest(shape=shape):
                    expected = answer(main_read_cycle_binding, folder)
                    self.assertEqual(answer(L.read_cycle_binding, folder), expected)
                    if shape in ("link-into-unsearchable", "link-target-too-long"):
                        if sys.version_info[:2] in ((3, 11), (3, 12)) and refuses:
                            # These Pythons raise them from `exists()`, and the scan stops on them.
                            self.assertEqual(expected, ("PermissionError", 13) if shape == "link-into-unsearchable"
                                             else ("OSError", 36))
                    elif refuses or shape != "unreadable-file":
                        self.assertEqual(expected, INVALID)
            finally:
                os.chmod(shut, 0o700)
        marker.unlink()
        # A cycle folder this user cannot search: whatever the path questions say on this Python.
        replace_marker(marker, good_bytes)
        self.locked(folder)
        self.assertEqual(answer(L.read_cycle_binding, folder), answer(main_read_cycle_binding, folder))
        os.chmod(folder, 0o700)
        # The marker turns into a link to another cycle's binding, or goes, just after a read's first
        # lookup: the read answers the marker it first looked at, or that it is invalid, never the other ID.
        for change in ("link", "remove"):
            for read in (main_read_cycle_binding, L.read_cycle_binding):
                replace_marker(marker, good_bytes)
                swap, swapped = once(lambda kind: True, change, marker, other)
                with self.subTest(change=change, read=read.__name__), MarkerLookups(after=swap):
                    got = answer(read, folder)
                    self.assertEqual(len(swapped), 1)
                    self.assertIn(got, (("value", good), INVALID))
        replace_marker(marker, good_bytes)

    def test_marker_read_answers_the_file_its_descriptor_opened(self):
        folder = self.begin_cycles(1)[0]
        marker = folder / L.CYCLE_BINDING
        good_bytes = marker.read_bytes()
        good = json.loads(good_bytes)
        other = folder.parent / "other-binding.json"
        other.write_text(json.dumps(dict(good, cycle_id="cyc_" + "e" * 32)), encoding="utf-8")
        changes = {
            # just before the open: the open sees the change
            ("before", "remove"): ("value", None),
            ("before", "link"): INVALID,
            # just after the open: the descriptor still reads the file it opened
            ("after", "remove"): ("value", good),
            ("after", "link"): ("value", good),
            ("after", "replace"): ("value", good),
        }
        for (when, change), expected in changes.items():
            replace_marker(marker, good_bytes)
            act, done = once(lambda kind: kind == "open", change, marker, other)
            with self.subTest(when=when, change=change), MarkerLookups(**{when: act}):
                self.assertEqual(answer(L.read_cycle_binding, folder), expected)
                self.assertEqual(done, ["open"])
        replace_marker(marker, good_bytes)

    def test_invalid_marker_still_stops_the_scan(self):
        folder = self.begin_cycles(1)[0]
        cycle_id = L.read_cycle_binding(folder)["cycle_id"]
        # A sealed cycle's manifest names the cycle, so the folder is still read as one.
        if not (folder / "manifest.json").exists():
            (folder / "manifest.json").write_text(json.dumps({"cycle": {"cycle_id": cycle_id}}), encoding="utf-8")
        (folder / L.CYCLE_BINDING).write_text("[]", encoding="utf-8")
        with self.assertRaises(L.LocatorError) as caught:
            OPS._scan_layout(self.root)
        self.assertEqual(caught.exception.code, "locator-cycle-binding-invalid")

    def test_marker_broken_after_its_first_scan_read_stops_the_scan(self):
        folder = self.begin_cycles(1)[0]
        marker = folder / L.CYCLE_BINDING
        state, read_patch, scan_patch = self.armed_in_scan(folder, lambda: replace_marker(marker, b"[]"))
        with read_patch, scan_patch, self.assertRaises(L.LocatorError) as caught:
            OPS._scan_layout(self.root)
        self.assertTrue(state["done"])
        self.assertEqual(caught.exception.code, "locator-cycle-binding-invalid")

    def test_marker_mended_after_its_first_scan_read_lets_the_scan_finish(self):
        folder = self.begin_cycles(1)[0]
        marker = folder / L.CYCLE_BINDING
        good_bytes = marker.read_bytes()
        cycle_id = json.loads(good_bytes)["cycle_id"]
        if not (folder / "manifest.json").exists():
            (folder / "manifest.json").write_text(json.dumps({"cycle": {"cycle_id": cycle_id}}), encoding="utf-8")
        replace_marker(marker, b"[]")
        state, read_patch, scan_patch = self.armed_in_scan(folder, lambda: replace_marker(marker, good_bytes))
        with read_patch, scan_patch:
            scan = OPS._scan_layout(self.root)
        self.assertTrue(state["done"])
        self.assertTrue(scan.complete, scan.blockers)
        self.assertEqual(scan.cycles.get(cycle_id), folder)

    def test_marker_broken_after_its_first_scan_read_leaves_hand_changes_for_the_next_look(self):
        kept, moved = self.begin_cycles(2, finish=True)
        moved_id = L.read_cycle_binding(moved)["cycle_id"]
        locator_before = P.read_cycle_record(self.root, moved_id)["locator"]
        moved.rename(moved.with_name("renamed-by-hand"))
        marker = kept / L.CYCLE_BINDING
        good_bytes = marker.read_bytes()
        state, read_patch, scan_patch = self.armed_in_scan(kept, lambda: replace_marker(marker, b"[]"))
        with read_patch, scan_patch:
            looked = P.reconcile_root(self.root)
        self.assertTrue(state["done"])
        self.assertEqual((looked["status"], looked.get("reason")), ("skipped", "LocatorError"), looked)
        self.assertEqual(P.read_cycle_record(self.root, moved_id)["locator"], locator_before)
        replace_marker(marker, good_bytes)
        self.assertEqual(P.reconcile_root(self.root)["status"], "reconciled")
        self.assertEqual(P.read_cycle_record(self.root, moved_id)["locator"], "renamed-by-hand")


if __name__ == "__main__":
    unittest.main()
