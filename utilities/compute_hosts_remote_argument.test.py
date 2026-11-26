"""A big remote script rides standard input, not the one-argument limit of ssh.

A host with many running reservations makes the launch script (it embeds the
GPU observation) larger than one argv word may be (131072 bytes on Linux);
`execve` then failed with E2BIG and nobody could start a run there. These
checks use a fake `ssh` executable and a synthetic observation: no real host,
no real GPU, no model.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import importlib.util
import io
import json
import shlex
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent
TOOL = ROOT / "compute-hosts.py"
ARG_LIMIT = 131072

FAKE_SSH = r"""#!/bin/sh
# Stands in for ssh: record what arrived as arguments and on stdin, then answer.
while [ "$#" -gt 0 ]; do
  case "$1" in -o|-p|-F) shift 2 ;; -*) shift ;; *) shift; break ;; esac
done
: > "$FAKE_SSH_DIR/arg-lengths"
for word in "$@"; do printf '%s\n' "${#word}" >> "$FAKE_SSH_DIR/arg-lengths"; done
printf '%s' "$*" > "$FAKE_SSH_DIR/command"
cat > "$FAKE_SSH_DIR/stdin"
case "$FAKE_SSH_MODE" in
  fail) echo "boom from remote" >&2; exit 3 ;;
  hang) exec sleep 30 ;;
  exec) bash -lc "$*" < "$FAKE_SSH_DIR/stdin" ;;
  *) printf '%s\n' "$FAKE_SSH_OUT" ;;
esac
"""

FAKE_TMUX = r"""#!/bin/sh
if [ "$1" = "-L" ]; then shift 2; fi
if [ "$1" = "start-server" ]; then exit 0; fi
[ "$1" = "new-session" ] || exit 1
shift
cwd=/
while [ "$#" -gt 0 ]; do
  case "$1" in
    -d) shift ;;
    -s) shift 2 ;;
    -c) cwd=$2; shift 2 ;;
    *) command=$1; break ;;
  esac
done
cd "$cwd" || exit 1
exec /bin/sh -c "$command"
"""


def load_module():
    spec = importlib.util.spec_from_file_location("compute_hosts_remote_argument", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def big_observation(reservations=200, processes=48, cmdline=6000):
    """One GPU with many reservations and long process command lines."""
    leases = [{"token": "lease-%04d" % number, "gpus": ["0"], "pid": 1000 + number,
               "owner": {"kind": "session", "harness": "claude", "id": "s-%04d" % number},
               "task": "synthetic task %d " % number + "x" * 200, "share": True}
              for number in range(reservations)]
    procs = [{"pid": 5000 + number, "used_memory_mib": 100,
              "command": "python train.py " + "--flag=%d " % number * (cmdline // 10)}
             for number in range(processes)]
    return {"reachable": True, "gpus": [{
        "index": 0, "uuid": "GPU-synthetic", "name": "Synthetic GPU",
        "utilization_gpu_pct": 0, "memory_used_mib": 0, "memory_total_mib": 24000,
        "processes": procs, "reservations": leases}]}


class RemoteArgumentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.record = self.root / "record"
        self.record.mkdir()
        (self.bin / "ssh").write_text(FAKE_SSH, encoding="utf-8")
        (self.bin / "tmux").write_text(FAKE_TMUX, encoding="utf-8")
        for name in ("ssh", "tmux"):
            (self.bin / name).chmod(0o755)
        self.run_root = self.root / "runs"
        self.host = {"ssh_host": "fixture-remote"}
        self.module = load_module()
        patched = mock.patch.dict(os.environ, {
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_SSH_DIR": str(self.record), "FAKE_SSH_MODE": "stub",
            "FAKE_SSH_OUT": json.dumps({"gpus": "0", "gpu_lease": None}),
            "TMPDIR": str(self.root)})
        patched.start()
        self.addCleanup(patched.stop)

    def arg_lengths(self):
        return [int(line) for line in (self.record / "arg-lengths").read_text().split()]

    def cmd_run(self, observation, command=("python", "-c", "pass"), **overrides):
        module = self.module
        args = SimpleNamespace(host="here", command=list(command), name="e2big-fixture",
                               cwd=None, env=None, gpus=1, share=True,
                               dry_run=False, json=True)
        for key, value in overrides.items():
            setattr(args, key, value)
        err = io.StringIO()
        with mock.patch.object(module, "load_config", return_value={"run_root": self.run_root}), \
             mock.patch.object(module, "_select", return_value=[("here", self.host)]), \
             mock.patch.object(module, "_run_gpu_observation", return_value=observation), \
             mock.patch.object(module, "_launcher_provenance", return_value={}), \
             mock.patch.object(module, "_launcher_route", return_value=None), \
             mock.patch.object(module, "_launcher_session_owner", return_value=None), \
             mock.patch.object(module, "_resource_parent", return_value=None), \
             mock.patch.object(module, "_select_payload_agent_home",
                               return_value=("", "absent", "fixture")), \
             mock.patch.object(module, "_spawn_completion_watch", return_value=False), \
             mock.patch("sys.stdout", new=io.StringIO()), mock.patch("sys.stderr", new=err):
            status = module.cmd_run(args)
        return status, err.getvalue()

    def test_big_observation_registers_and_the_body_rides_stdin(self):
        observation = big_observation()
        status, _err = self.cmd_run(observation)
        self.assertEqual(status, 0)
        body = (self.record / "stdin").read_text(encoding="utf-8")
        old_argument = len(shlex.quote("python3 -c " + shlex.quote(body)).encode())
        self.assertGreater(len(body), 300_000)            # the script that used to be one argument
        self.assertGreater(old_argument, ARG_LIMIT)       # ... and would have failed E2BIG
        self.assertIn("lease-0199", body)                 # the observation reached the host
        self.assertIn("e2big-fixture", body)
        self.assertEqual((self.record / "command").read_text(), "bash -lc 'python3 -'")
        self.assertTrue(all(length < 100 for length in self.arg_lengths()), self.arg_lengths())
        (run_dir,) = list(self.run_root.iterdir())
        self.assertEqual(json.loads((run_dir / "meta.json").read_text())["gpus"], "0")

    def test_the_observation_reaches_the_host_unchanged(self):
        observation = big_observation(reservations=3, processes=1, cmdline=40)
        self.cmd_run(observation)
        body = (self.record / "stdin").read_text(encoding="utf-8")
        compile(body, "<launch>", "exec")                 # still one valid script
        self.assertIn("'observation': " + repr(observation), body)
        self.assertIn("'requested': 1, 'share': True", body)

    def test_failure_and_timeout_keep_their_shape(self):
        module = self.module
        with mock.patch.dict(os.environ, {"FAKE_SSH_MODE": "fail"}):
            done = module.remote(self.host, "python3 -", input="print(1)\n")
        self.assertEqual((done.returncode, done.stdout, done.stderr),
                         (3, "", "boom from remote\n"))
        with mock.patch.dict(os.environ, {"FAKE_SSH_MODE": "hang"}):
            done = module.remote(self.host, "python3 -", timeout=1, input="print(1)\n")
            plain = module.remote(self.host, "true", timeout=1)
        for result in (done, plain):
            self.assertEqual(result.returncode, 255)
            self.assertTrue(result.stderr.endswith("timed out"))
        with mock.patch.dict(os.environ, {"FAKE_SSH_MODE": "fail"}):
            status, err = self.cmd_run(big_observation(reservations=2, processes=1, cmdline=40))
        self.assertEqual(status, 1)
        self.assertIn("launch failed on here: boom from remote; diagnostics:", err)

    def test_command_too_long_for_one_argument_is_one_plain_line(self):
        done = self.module.remote(self.host, "echo " + "x" * 200_000)
        self.assertEqual(done.returncode, 126)
        self.assertEqual(len(done.stderr.splitlines()), 1)
        self.assertRegex(done.stderr, r"^argument too long: .* 2000\d\d bytes in one argument "
                                      r"\(the limit is about 131072 bytes\)")

    def test_many_owner_claims_in_the_probe_ride_stdin(self):
        claims = [{"kind": "session", "harness": "claude", "id": "claim-%05d" % n,
                   "pid": n, "note": "y" * 120} for n in range(1500)]
        payload = {"hostname": "remote", "gpus": [], "observed_at": 1.0}
        with mock.patch.dict(os.environ, {"FAKE_SSH_OUT": json.dumps(payload)}):
            row = self.module.probe_host("remote", self.host, owner_claims=claims,
                                         ssh_session_bridges=[])
        self.assertTrue(row["reachable"], row)
        body = (self.record / "stdin").read_text(encoding="utf-8")
        self.assertGreater(len(body), ARG_LIMIT)
        self.assertIn("claim-01499", body)
        self.assertEqual((self.record / "command").read_text(), "bash -lc 'bash -s'")

    def test_real_execution_keeps_quotes_newlines_dollar_and_hangul(self):
        workdir = self.root / "작업 dir"
        workdir.mkdir()
        probe = self.root / "probe.txt"
        script = ("import os, sys\n"
                  "open(sys.argv[1], 'w').write(repr((sys.argv[2], os.getcwd(), os.environ.get('HOME'))))\n")
        weird = "it's \"quoted\"\nline2 $HOME `x` 한글"
        config = self.root / "compute-hosts.yaml"
        config.write_text("schema_version: 1\nrun_root: %s\nhosts:\n  remote:\n"
                          "    ssh_host: fixture-remote\n" % self.run_root, encoding="utf-8")
        env = {"PATH": os.environ["PATH"], "HOME": str(self.root / "home"),
               "XDG_STATE_HOME": str(self.root / "state"),
               "HARNESS_STATE_ROOT": str(self.root / "hstate"),
               "COMPUTE_HOSTS_CONFIG": str(config), "TMPDIR": str(self.root),
               "FAKE_SSH_DIR": str(self.record), "FAKE_SSH_MODE": "exec",
               "LC_ALL": "C.UTF-8"}
        (self.root / "home").mkdir()
        done = subprocess.run(
            [sys.executable, str(TOOL), "run", "remote", "--gpus", "", "--cwd", str(workdir),
             "--name", "quoting", "--", sys.executable, "-c", script, str(probe), weird],
            text=True, capture_output=True, env=env)
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        run_id = done.stdout.split()[1]
        exit_path = self.run_root / run_id / "exit_code"
        for _ in range(100):
            if exit_path.is_file():
                break
            time.sleep(0.1)
        self.assertEqual(exit_path.read_text().strip(), "0",
                         (self.run_root / run_id / "log").read_text())
        self.assertEqual(eval(probe.read_text(encoding="utf-8")),
                         (weird, str(workdir), str(self.root / "home")))


if __name__ == "__main__":
    unittest.main()
