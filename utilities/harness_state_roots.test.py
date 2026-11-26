#!/usr/bin/env python3
"""Which harness state a launch writes is one rule, read the same way by every adapter (audit §4 #8, A7)."""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import argparse
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import harness_state_roots as H  # noqa: E402
from dispatch_contract import dispatch_state_root  # noqa: E402
from dispatch_summary import summary_writable_dirs  # noqa: E402


class HarnessStateRootsTest(unittest.TestCase):
    def test_recursive_owner_can_append_route_history_without_granting_it_to_workers(self):
        ledger = Path(self._tmp.name) / "route-chains"
        with mock.patch.dict("os.environ", {"FLEET_ROUTE_CHAIN_DIR": str(ledger)}):
            owner = self.args(nested_headless_network=True)
            self.assertIn(ledger, H.route_bound_worker_writable_dirs(owner))
            worker = self.args(route_id="rt-worker", nested_headless_network=False)
            self.assertNotIn(ledger, H.route_bound_worker_writable_dirs(worker))

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        (self.home / ".core-grounding").mkdir(parents=True)
        self.jobs = Path(self._tmp.name) / "state" / "jobs.log"
        self.state_root = Path(self._tmp.name) / "hearting-state"
        self._environment = mock.patch.dict(os.environ, {
            "HARNESS_STATE_ROOT": str(self.state_root),
            "AGENT_HOME": str(self.home),
        })
        self._environment.start()
        self.addCleanup(self._environment.stop)

    def args(self, **values):
        base = dict(agent_home=str(self.home), jobs_path=str(self.jobs))
        base.update(values)
        return argparse.Namespace(**base)

    def test_each_rule(self):
        plain, routed = self.args(), self.args(route_id="rt-1", command_attempt_id="att-1")
        registered = self.args(execution_surface="registered-headless", registered_worker=1)
        root = dispatch_state_root(str(self.jobs))
        self.assertEqual(H.progress_writable_dirs(plain), (root / "heartbeats", root / "watchdog"))
        self.assertEqual(H.progress_writable_dirs(self.args(jobs_path=None)), ())
        self.assertEqual(H.route_bound_worker_writable_dirs(plain), ())
        self.assertEqual(H.route_bound_worker_writable_dirs(routed), ((self.home / ".core-grounding").resolve(),))
        self.assertEqual([H.spec_read_marker_required(a) for a in (plain, routed, registered)], [False, True, True])
        self.assertEqual([H.registry_writable_launch(a) for a in (plain, routed, self.args(route_id="rt-1"))],
                         [False, True, False])
        self.assertEqual(H.spec_grounding_dir(plain), self.state_root / "spec-grounding")

    def test_spec_marker_root_is_release_independent(self):
        first = self.args(agent_home=str(self.home / "release-a"))
        second = self.args(agent_home=str(self.home / "release-b"))
        with mock.patch.dict(os.environ, {"HARNESS_STATE_ROOT": str(self.state_root)}):
            a = H.spec_grounding_dir(first)
            b = H.spec_grounding_dir(second)
        self.assertEqual(a, self.state_root / "spec-grounding")
        self.assertEqual(a, b)
        for env, expected in (
            ({"XDG_STATE_HOME": str(self._tmp.name) + "/xdg", "HOME": str(self.home)},
             Path(self._tmp.name) / "xdg" / "hearting" / "spec-grounding"),
            ({"HOME": str(self.home)},
             self.home / ".local" / "state" / "hearting" / "spec-grounding"),
        ):
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(H.spec_grounding_dir(first), expected)

    def test_the_codex_wrapper_projects_the_shared_answer(self):
        spec = importlib.util.spec_from_file_location(
            "codex_dispatch_headless_state_roots", ROOT / "adapters/codex/bin/dispatch-headless.py")
        codex = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(codex)
        for name, shared in (("progress_writable_dirs", H.progress_writable_dirs),
                             ("route_bound_worker_writable_dirs", H.route_bound_worker_writable_dirs),
                             ("nested_opencode_store_writable_dirs", H.nested_opencode_store_writable_dirs),
                             ("spec_read_marker_required", H.spec_read_marker_required),
                             ("registry_writable_launch", H.registry_writable_launch),
                             ("_spec_grounding_dir", H.spec_grounding_dir),
                             ("_core_grounding_dir", H.core_grounding_dir),
                             ("summary_writable_dirs", summary_writable_dirs)):
            with self.subTest(name=name):
                self.assertIs(getattr(codex, name), shared)
        ledger = Path(self._tmp.name) / "fresh-ledger"
        titles = Path(self._tmp.name) / "titles"
        with mock.patch.dict("os.environ", {"FLEET_ROUTE_CHAIN_DIR": str(ledger)}), \
                mock.patch.object(codex, "summary_writable_dirs", return_value=(titles,)):
            self.assertFalse(ledger.exists())
            codex.ensure_owner_writable_dirs(self.args(nested_headless_network=True))
            self.assertTrue(ledger.is_dir())
            self.assertTrue(titles.is_dir())

        worker = self.args(route_id="rt-spec", command_attempt_id="att-spec")
        with mock.patch.object(codex, "nested_owner_writable_dirs", return_value=()):
            codex.ensure_owner_writable_dirs(worker)
        marker_dir = self.state_root / "spec-grounding"
        self.assertTrue(marker_dir.is_dir())
        self.assertEqual(marker_dir.stat().st_mode & 0o777, 0o700)
        self.assertFalse((self.home / ".spec-grounding").exists())

    def test_only_a_child_spawning_owner_opens_the_three_native_store_roots(self):
        xdg = Path(self._tmp.name) / "xdg"
        env = {"HOME": str(self.home), "XDG_DATA_HOME": str(xdg / "data"),
               "XDG_STATE_HOME": str(xdg / "state"), "XDG_CACHE_HOME": str(xdg / "cache")}
        owner = self.args(worker_type="owner", nested_headless_network=True,
                          nested_headless_network_eligible=True)
        with mock.patch.dict("os.environ", env, clear=True):
            roots = H.nested_opencode_store_writable_dirs(owner)
            self.assertEqual(roots, tuple((Path(env[key]) / "opencode").resolve()
                                          for key in ("XDG_DATA_HOME", "XDG_STATE_HOME",
                                                      "XDG_CACHE_HOME")))
            self.assertEqual(H.nested_opencode_store_writable_dirs(
                self.args(worker_type="stage", nested_headless_network_eligible=True)), ())
            self.assertEqual(H.nested_opencode_store_writable_dirs(
                self.args(worker_type="owner", nested_headless_network=False,
                          nested_headless_network_eligible=False)), ())
            codex_spec = importlib.util.spec_from_file_location(
                "codex_dispatch_headless_state_roots", ROOT / "adapters/codex/bin/dispatch-headless.py")
            codex = importlib.util.module_from_spec(codex_spec)
            codex_spec.loader.exec_module(codex)
            self.assertTrue(set(roots).issubset(set(codex.nested_owner_writable_dirs(owner))))
            with mock.patch.object(codex, "summary_writable_dirs", return_value=()):
                codex.ensure_owner_writable_dirs(owner)
            self.assertTrue(all(path.is_dir() for path in roots))

    def test_opencode_store_symlink_is_not_expanded_into_a_wider_writable_root(self):
        xdg = Path(self._tmp.name) / "xdg"
        target = self.home / ".config" / "opencode"
        target.mkdir(parents=True)
        data_root = xdg / "data" / "opencode"
        data_root.parent.mkdir(parents=True)
        data_root.symlink_to(target, target_is_directory=True)
        env = {"HOME": str(self.home), "XDG_DATA_HOME": str(xdg / "data"),
               "XDG_STATE_HOME": str(xdg / "state"), "XDG_CACHE_HOME": str(xdg / "cache")}
        owner = self.args(worker_type="owner", nested_headless_network=True,
                          nested_headless_network_eligible=True)
        with mock.patch.dict("os.environ", env, clear=True):
            roots = H.nested_opencode_store_writable_dirs(owner)
            self.assertEqual(roots, (xdg / "state/opencode", xdg / "cache/opencode"))
            self.assertNotIn(target, roots)
            codex_spec = importlib.util.spec_from_file_location(
                "codex_dispatch_headless_state_roots_symlink", ROOT / "adapters/codex/bin/dispatch-headless.py")
            codex = importlib.util.module_from_spec(codex_spec)
            codex_spec.loader.exec_module(codex)
            with mock.patch.object(codex, "summary_writable_dirs", return_value=()):
                writable = codex.nested_owner_writable_dirs(owner)
            self.assertNotIn(target, writable)
            self.assertNotIn(data_root.resolve(), writable)

    def test_each_store_alias_of_xdg_config_is_excluded_without_creating_paths(self):
        xdg = Path(self._tmp.name) / "aliased-xdg"
        config = xdg / "config"
        owner = self.args(worker_type="owner", nested_headless_network_eligible=True)
        env = {"HOME": str(self.home), "XDG_CONFIG_HOME": str(config),
               "XDG_DATA_HOME": str(xdg / "data"), "XDG_STATE_HOME": str(xdg / "state"),
               "XDG_CACHE_HOME": str(xdg / "cache")}
        keys = ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")
        for key in keys:
            with self.subTest(store=key):
                selected = {**env, key: str(config)}
                expected = tuple(Path(selected[other]) / "opencode" for other in keys if other != key)
                self.assertEqual(H.nested_opencode_store_writable_dirs(owner, selected), expected)
        self.assertFalse(xdg.exists())

    def test_store_cannot_contain_or_live_inside_native_config(self):
        xdg = Path(self._tmp.name) / "nested-xdg"
        owner = self.args(worker_type="owner", nested_headless_network_eligible=True)
        env = {"HOME": str(self.home), "XDG_DATA_HOME": str(xdg / "data"),
               "XDG_CONFIG_HOME": str(xdg / "config"),
               "XDG_STATE_HOME": str(xdg / "state"), "XDG_CACHE_HOME": str(xdg / "cache")}
        cases = (
            {"XDG_CONFIG_HOME": str(xdg / "data/opencode/nested")},
            {"XDG_DATA_HOME": str(xdg / "config/opencode/nested")},
            {"OPENCODE_CONFIG_DIR": str(xdg / "data/opencode")},
            {"OPENCODE_CONFIG": str(xdg / "data/opencode/custom.json")},
            {"HOME": str(xdg / "opencode"), "XDG_DATA_HOME": str(xdg)},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                self.assertEqual(H.nested_opencode_store_writable_dirs(owner, {**env, **overrides}),
                                 (xdg / "state/opencode", xdg / "cache/opencode"))
        self.assertFalse(xdg.exists())

    def test_config_base_alias_and_a_nested_config_symlink_do_not_grant_config(self):
        base = Path(self._tmp.name)
        config_base = base / "native-config"
        config_base.mkdir()
        data_alias = base / "data-alias"
        data_alias.symlink_to(config_base, target_is_directory=True)
        env = {"HOME": str(self.home), "XDG_CONFIG_HOME": str(config_base),
               "XDG_DATA_HOME": str(data_alias), "XDG_STATE_HOME": str(base / "state"),
               "XDG_CACHE_HOME": str(base / "cache")}
        owner = self.args(worker_type="owner", nested_headless_network_eligible=True)
        expected = (base / "state/opencode", base / "cache/opencode")
        self.assertEqual(H.nested_opencode_store_writable_dirs(owner, env), expected)
        store = base / "data/opencode"
        nested = store / "inner"
        nested.mkdir(parents=True)
        (nested / "opencode").symlink_to(config_base, target_is_directory=True)
        self.assertEqual(H.nested_opencode_store_writable_dirs(owner, {
            **env, "XDG_DATA_HOME": str(base / "data"), "XDG_CONFIG_HOME": str(nested)}), expected)

    def test_codex_projection_keeps_a_config_alias_outside_its_grants(self):
        xdg = Path(self._tmp.name) / "projection-xdg"
        owner = self.args(worker_type="owner", nested_headless_network=True,
                          nested_headless_network_eligible=True)
        config = xdg / "config/opencode"
        spec = importlib.util.spec_from_file_location(
            "codex_dispatch_config_alias", ROOT / "adapters/codex/bin/dispatch-headless.py")
        codex = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(codex)
        env = {"HOME": str(self.home), "XDG_CONFIG_HOME": str(xdg / "config"),
               "XDG_DATA_HOME": str(xdg / "config"), "XDG_STATE_HOME": str(xdg / "state"),
               "XDG_CACHE_HOME": str(xdg / "cache")}
        with mock.patch.dict("os.environ", env, clear=True), \
                mock.patch.object(codex, "summary_writable_dirs", return_value=()):
            self.assertNotIn(config, codex.nested_owner_writable_dirs(owner))
            codex.ensure_owner_writable_dirs(owner)
        self.assertFalse(config.exists())
        self.assertTrue((xdg / "state/opencode").is_dir())
        self.assertTrue((xdg / "cache/opencode").is_dir())

    def test_only_an_adapter_that_confines_writes_opens_them(self):
        # The rule is shared; opening the directories is the realization of an
        # adapter whose declared enforcement confines writes.
        declared = {h: json.loads((ROOT / "adapters" / h / "config" / "harness-capabilities.json")
                                  .read_text(encoding="utf-8"))["access"]["enforcement"]
                    for h in ("claude", "codex", "opencode")}
        self.assertEqual({h for h, e in declared.items() if e == "os-sandbox"}, {"codex"})


if __name__ == "__main__":
    unittest.main()
