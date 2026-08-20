#!/usr/bin/env python3
"""Hermetic unit tests — F-28c (governor half, plan §6a). Real state.json shape fixture,
tempfile-only — never touches the real `.runtime/model-worker-governor/`.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet.collectors import governor    # noqa: E402
from fleet.collectors import procscan    # noqa: E402


def _write_state(tmp, leases, reservations=None):
    path = os.path.join(tmp, "state.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"schema_version": 2, "leases": leases,
                   "reservations": reservations or {}, "claims": {}, "starts": []}, f)
    return tmp


class GovernorCollectTest(unittest.TestCase):
    def setUp(self):
        governor.clear_cache()

    def tearDown(self):
        governor.clear_cache()

    def test_zombie_proc_start_is_not_live_identity_evidence(self):
        fields = ["Z"] + [str(value) for value in range(4, 22)] + ["98765"]
        stat = "4242 (worker with parens)) " + " ".join(fields)
        with mock.patch("builtins.open", mock.mock_open(read_data=stat)):
            self.assertIsNone(procscan.read_proc_start(4242))

    def test_alive_lease_counted(self):
        with tempfile.TemporaryDirectory() as td:
            _write_state(td, {"t1": {"class": "dispatch", "pid": 111, "starttime": "222"}})
            with mock.patch.object(procscan, "read_proc_start", return_value="222"):
                result = governor.collect(root=td)
        self.assertEqual(result, {"active": 1, "cap": governor.DEFAULT_TOTAL_LIMIT, "classes": {"dispatch": 1}})

    def test_dead_lease_never_write_state_json_but_excluded_from_count(self):
        with tempfile.TemporaryDirectory() as td:
            _write_state(td, {
                "t1": {"class": "dispatch", "pid": 111, "starttime": "222"},   # alive
                "t2": {"class": "dispatch", "pid": 999, "starttime": "111"},   # dead (mismatch)
            })
            with open(os.path.join(td, "state.json"), "rb") as f:
                before = f.read()
            with mock.patch.object(procscan, "read_proc_start",
                                   side_effect=lambda pid: "222" if pid == 111 else "999"):
                result = governor.collect(root=td)
            with open(os.path.join(td, "state.json"), "rb") as f:
                after = f.read()
        self.assertEqual(result["active"], 1)   # only the alive one counted
        self.assertEqual(before, after)          # ★ read-only — file bytes untouched

    def test_live_atomic_reservations_count_as_occupied_capacity(self):
        with tempfile.TemporaryDirectory() as td:
            _write_state(
                td,
                {"lease": {"class": "dispatch", "pid": 111, "starttime": "aaa"}},
                {
                    "live": {"class": "dispatch", "owner_pid": 222,
                             "owner_starttime": "bbb"},
                    "stale": {"class": "dispatch", "owner_pid": 333,
                              "owner_starttime": "ccc"},
                },
            )
            starts = {111: "aaa", 222: "bbb", 333: "reused"}
            with mock.patch.object(
                procscan, "read_proc_start", side_effect=lambda pid: starts.get(pid)
            ):
                result = governor.collect(root=td)
        self.assertEqual(result, {"active": 2, "cap": governor.DEFAULT_TOTAL_LIMIT, "classes": {"dispatch": 2}})

    def test_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            result = governor.collect(root=os.path.join(td, "nope"))
        self.assertIsNone(result)

    def test_corrupt_json_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "state.json"), "w") as f:
                f.write("{not json")
            result = governor.collect(root=td)
        self.assertIsNone(result)

    def test_cache_hit_skips_second_json_load(self):
        with tempfile.TemporaryDirectory() as td:
            _write_state(td, {"t1": {"class": "dispatch", "pid": 111, "starttime": "222"}})
            with mock.patch.object(procscan, "read_proc_start", return_value="222"):
                with mock.patch("fleet.collectors.governor.json.load", wraps=json.load) as m:
                    governor.collect(root=td)
                    governor.collect(root=td)
                    self.assertEqual(m.call_count, 1)

    def test_env_override_total_limit(self):
        with tempfile.TemporaryDirectory() as td:
            _write_state(td, {})
            with mock.patch.dict(os.environ, {"AGENT_MODEL_WORKER_TOTAL": "9"}):
                result = governor.collect(root=td)
        self.assertEqual(result["cap"], 9)


if __name__ == "__main__":
    unittest.main()
