#!/usr/bin/env python3
"""Real projected carrier: arm, prune its release, then finish the owner."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import ast
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixture_env

REPO = Path(__file__).resolve().parents[2]
CARRIER = '''import importlib.util, pathlib, sys
root = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fixture_rewake", root / "hooks/dispatch-owner-rewake.py")
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)
jobs = pathlib.Path(sys.argv[1])
claim = mod.ArmClaim(jobs.parent / "rewake-arms/att-fixture.json", "att-fixture", "session-fixture", 1,
                     mod._process_identity(__import__("os").getpid()))
def wait(*args):
    print("armed", flush=True)
    sys.stdin.readline()
    import dispatch_notice_state
    jobs.with_name("delivered").write_text("owner completed")
    return 2
mod._run_carrier = wait
raise SystemExit(mod._observe_carrier(mod.Launch("att-fixture", jobs, "session-fixture"), claim, {}))
'''


class CarrierReleasePruneTest(unittest.TestCase):
    def run_case(self, before=False, legacy=False):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            share = base / "share"
            releases = share / "releases"
            old = releases / "old"
            shutil.copytree(REPO / "utilities", old / "utilities",
                            ignore=shutil.ignore_patterns("*.test.*", "__pycache__"))
            (old / "hooks").mkdir()
            (old / "hooks/dispatch-owner-rewake.py").write_bytes(
                (REPO / "hooks/dispatch-owner-rewake.py").read_bytes())
            if before or legacy:
                contract = old / "utilities/dispatch_contract.py"
                tree = ast.parse(contract.read_text())
                tree.body = [n for n in tree.body if not (
                    isinstance(n, ast.Assign) and any(isinstance(t, ast.Name)
                        and t.id == "_CODE_ROOT_FD" for t in n.targets))]
                contract.write_text(ast.unparse(tree))
            (old / "hooks/carrier.py").write_text(CARRIER)
            projection = base / "projection"
            projection.symlink_to(old / "hooks")
            jobs = base / "state/jobs.log"
            jobs.parent.mkdir()
            jobs.write_text("")
            env = {k: v for k, v in os.environ.items() if k not in {
                "AGENT_HOME", "CLAUDE_HOME", "AGENT_DISPATCH_JOBS", "PYTHONPATH"}}
            proc = subprocess.Popen([sys.executable, str(projection / "carrier.py"), str(jobs)],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, env=env)
            try:
                self.assertTrue(select.select([proc.stdout], [], [], 15)[0])
                self.assertEqual(proc.stdout.readline().strip(), "armed")
                newer = releases / "newer"
                newer.mkdir()
                (newer / "carrier.py").write_text("pass")
                # destructive-ok: reason=rotate this test's temporary projection; boundary=projection under TemporaryDirectory
                projection.unlink()
                projection.symlink_to(newer)
                (releases / "current").mkdir()
                os.utime(old, (1, 1))
                scanner = distribution._release_held_by_live_process
                if before:
                    # Previous judgment looked only for literal environment/argv
                    # references. Keep that mechanism reproducible in shallow CI.
                    def scanner(candidate):
                        for name in ("cmdline", "environ"):
                            fields = Path(f"/proc/{proc.pid}/{name}").read_text().split("\0")
                            for field in fields:
                                value = field.split("=", 1)[-1] if name == "environ" else field
                                if value == str(candidate) or value.startswith(str(candidate) + "/"):
                                    return True, "literal-reference"
                        return False, ""
                with ExitStack() as stack:
                    for name, value in {
                        "data_root": share, "_stable_registry_snapshot": [],
                        "_open_route_launch_homes": ([], ""), "_release_in_use": (False, ""),
                        "_release_projection_referenced": False, "_succeed_dispatch_state": True,
                        "_migration_deletion_precondition": (True, ""),
                        "_retention_containment_precondition": (True, ""),
                    }.items():
                        stack.enter_context(mock.patch.object(distribution, name, return_value=value))
                    stack.enter_context(mock.patch.object(distribution, "_release_held_by_live_process", scanner))
                    distribution._cleanup_releases(set())
                    self.assertEqual(old.exists(), not before)
                    out, err = proc.communicate("owner done\n", timeout=15)
                    if before:
                        self.assertEqual(proc.returncode, 1, err)
                        self.assertIn("ModuleNotFoundError", err)
                        self.assertFalse(jobs.with_name("delivered").exists())
                    else:
                        self.assertEqual(proc.returncode, 2, err)
                        self.assertTrue(jobs.with_name("delivered").exists())
                        distribution._cleanup_releases(set())
                        self.assertFalse(old.exists(), "finished carriers must not pin releases")
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.communicate(timeout=5)

    def test_before_late_import_fails_and_completion_is_lost(self):
        self.run_case(before=True)

    def test_after_actual_import_release_survives_rotation_and_prune(self):
        self.run_case()

    def test_pre_upgrade_projected_carrier_is_kept_until_exit(self):
        self.run_case(legacy=True)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="hearting-carrier-release-prune-") as temp:
        fixture_root = Path(temp).resolve()
        with fixture_env.patched_environment(fixture_root / "runtime", REPO) as env:
            private_root = (fixture_root / "runtime").resolve()
            for key in (
                "HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
                "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "HARNESS_STATE_ROOT",
                "AGENT_DISPATCH_JOBS", "AGENT_ARTIFACT_ROOT",
            ):
                Path(env[key]).resolve().relative_to(private_root)
            print("routine_fixture_roots=" + json.dumps({
                key: env[key] for key in (
                    "HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "HARNESS_STATE_ROOT",
                    "AGENT_DISPATCH_JOBS", "AGENT_ARTIFACT_ROOT",
                )
            }, sort_keys=True), flush=True)
            import distribution
            unittest.main()
