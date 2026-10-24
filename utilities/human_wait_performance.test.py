from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "utilities"))
sys.path.insert(0, str(ROOT / "tools"))

import human_gate_observation as observation
import human_wait_state as wait_state
import dispatch_registry_cache as registry_cache
from route_identity import route_hash, route_id_from_hash
from fleet import herdr_projection
import session_tidy


class HumanWaitPerformanceTests(unittest.TestCase):
    """One-size 32 MiB fixture: real projection and cache-only surface timings."""

    def test_real_size_cache_and_warm_projection(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as temp:
            root = Path(temp)
            state = root / "state"
            os.environ["XDG_STATE_HOME"] = str(state)
            routes = root / "routes"
            routes.mkdir()
            now = time.time()
            filler = "x" * 4800
            route_rows = []
            for index in range(1067):
                route = {"artifact_root": str(root), "nodes": [], "fixture_route": index}
                digest = route_hash(route)
                route_id = route_id_from_hash(digest)
                route["route_hash"], route["route_id"] = digest, route_id
                route_file = routes / (route_id + ".json")
                route_file.write_text(json.dumps(route), encoding="utf-8")
                route_rows.append((route_id, digest, route_file))
            rows = []
            for index in range(6398):
                route_index = index % len(route_rows)
                route_id, digest, route_file = route_rows[route_index]
                route_time = now - (3600 if route_index < 380 else 30 * 86400)
                (routes / (route_id + ".outcome.json")).write_text(json.dumps({
                    "route_id": route_id, "route_hash": digest}), encoding="utf-8")
                metadata = ",".join(("attempt_schema_version=2", "dispatch_depth=2",
                    "worker_type=child", "attempt_id=att-fixture-%d" % index,
                    "route_id=" + route_id, "route_hash=" + digest, "route_file=" + str(route_file),
                    "ended_at=" + str(route_time), "padding=" + filler))
                rows.append(f"{route_time}\topen\t/repo\t/wt\tchild\t{metadata}\n")
            route_id, digest, route_file = route_rows[0]
            owner_meta = ",".join(("attempt_schema_version=2", "dispatch_depth=1",
                "worker_type=owner", "attempt_id=att-performance", "harness=codex",
                "parent_sid=sid-parent", "parent_harness=codex", "route_id=" + route_id,
                "route_hash=" + digest, "route_file=" + str(route_file),
                "owner_route_id=" + route_id, "owner_route_hash=" + digest,
                "owner_route_file=" + str(route_file), "ended_at=" + str(now - 600),
                "note=dead-worker-blocked", "failure_class=blocked",
                "padding=" + filler))
            rows.append(f"{now}\tdone\t/repo\t/wt\towner\t{owner_meta}\n")
            jobs = root / "jobs.log"
            jobs.write_text("".join(rows), encoding="utf-8")
            size = jobs.stat().st_size
            self.assertGreaterEqual(size, 31_000_000)
            self.assertLessEqual(size, 34_000_000)

            # Cold producer is a detached CLI child with its own XDG state and
            # explicitly selected fixture registry. No display caller waits on it.
            cold_state = root / "cold-state"
            empty_bin = root / "empty-bin"
            cold_state.mkdir()
            empty_bin.mkdir()
            producer_code = ("import sys; sys.path.insert(0," + repr(str(ROOT / "utilities")) + "); "
                            "import human_wait_state; raise SystemExit(human_wait_state.main(['--refresh-cache']))")
            cold_env = dict(os.environ, XDG_STATE_HOME=str(cold_state), HOME=str(root / "cold-home"),
                            AGENT_DISPATCH_JOBS=str(jobs), PATH=str(empty_bin), PYTHONDONTWRITEBYTECODE="1")
            cold_started = time.perf_counter()
            cold_child = subprocess.run([sys.executable, "-c", producer_code], env=cold_env,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            cold = time.perf_counter() - cold_started
            self.assertEqual(cold_child.returncode, 0, cold_child.stderr.decode(errors="replace"))
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(cold_state)}):
                cold_cache = wait_state._read_cache()
            self.assertIsNotNone(cold_cache)
            result = {"total": cold_cache["total"]}
            self.assertEqual(result["total"], 1)

            def projection():
                # Exercise the same read + private atomic publish as each loop pass.
                from tools.fleet.collectors import dispatch
                with mock.patch.object(dispatch, "human_gate_registry_paths", return_value=[jobs]), \
                     mock.patch.object(wait_state.shutil, "which", return_value=None):
                    if not wait_state._refresh_cache():
                        raise AssertionError("warm producer pass failed")
                value = wait_state.cached_view(request=False)
                if value is None:
                    raise AssertionError("producer cache was not readable")
                return value

            registry_cache.reset_metrics()
            parent_cold_started = time.perf_counter()
            projection()
            parent_cold = time.perf_counter() - parent_cold_started
            after_cold = registry_cache.metrics_snapshot()
            unchanged = []
            for _ in range(10):
                started = time.perf_counter()
                projection()
                unchanged.append(time.perf_counter() - started)
            after_warm = registry_cache.metrics_snapshot()
            with jobs.open("a", encoding="utf-8") as handle:
                handle.write(f"{now}\topen\t/repo\t/wt\tchild\t{{}}\n")
            append_started = time.perf_counter()
            projection()
            append = time.perf_counter() - append_started
            after_append = registry_cache.metrics_snapshot()
            self.assertEqual(after_cold["full_metadata_parses"], 1)
            self.assertEqual(after_warm["full_metadata_parses"], after_cold["full_metadata_parses"],
                             (after_cold, after_warm))
            self.assertGreater(after_append["delta_reads"], after_warm["delta_reads"])

            # Exercise the actual cache consumer helper. The baseline removes
            # only the answer-wait lookup; title/formatter work is identical.
            latency = {}
            env = dict(os.environ, XDG_STATE_HOME=str(state), HOME=str(root))
            for key in ("AGENT_SESSION_ROLE", "AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_DEPTH",
                        "FLEET_TITLE_REFRESH", "MEM_DISTILL"):
                env.pop(key, None)
            for key in tuple(env):
                if key.startswith("OPENCODE_DISPATCH"):
                    env.pop(key, None)
            for case, observed in (("present", now), ("stale-90", now - 90),
                                   ("missing", None), ("stale-240", now - 240)):
                raw, baseline = {"pane": [], "start": [], "prompt": []}, {
                    "pane": [], "start": [], "prompt": []}
                with mock.patch.object(wait_state, "_ensure_producer", return_value=True):
                    for _ in range(10):
                        raw_state = root / (case + "-raw-state")
                        raw_state.mkdir(exist_ok=True)
                        if observed is not None:
                            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(raw_state)}):
                                wait_state.publish_cache(dict(result, observed_at=observed))
                        started = time.perf_counter()
                        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(raw_state)}):
                            herdr_projection._metadata_command("herdr", "codex", "sid-parent", "pane")
                        raw["pane"].append(time.perf_counter() - started)
                        started = time.perf_counter()
                        with mock.patch.object(wait_state, "cached_view", return_value=None):
                            herdr_projection._metadata_command("herdr", "codex", "sid-parent", "pane")
                        baseline["pane"].append(time.perf_counter() - started)
                        for event in ("start", "prompt"):
                            raw_hook_state = root / (case + "-raw-hook-%d-%s" % (_, event))
                            baseline_hook_state = root / (case + "-baseline-hook-%d-%s" % (_, event))
                            raw_hook_state.mkdir(exist_ok=True)
                            baseline_hook_state.mkdir(exist_ok=True)
                            if observed is not None:
                                with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(raw_hook_state)}):
                                    wait_state.publish_cache(dict(result, observed_at=observed))
                            raw_env = dict(env, XDG_STATE_HOME=str(raw_hook_state))
                            baseline_env = dict(env, XDG_STATE_HOME=str(baseline_hook_state))
                            started = time.perf_counter()
                            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(baseline_hook_state)}), \
                                 mock.patch.object(wait_state, "cached_view", return_value=None):
                                session_tidy.run_hook("codex", event, "sid-perf", cwd=str(root),
                                                      env=baseline_env, now=now, emit=lambda _text: None)
                            baseline[event].append(time.perf_counter() - started)
                            started = time.perf_counter()
                            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(raw_hook_state)}):
                                session_tidy.run_hook("codex", event, "sid-perf", cwd=str(root),
                                                      env=raw_env, now=now, emit=lambda _text: None)
                            raw[event].append(time.perf_counter() - started)
                latency[case] = {surface: (raw[surface], baseline[surface]) for surface in raw}

            child_code = ("import sys; sys.path.insert(0," + repr(str(ROOT / "utilities")) + "); "
                          "import human_wait_state as h\noriginal=h._refresh_cache\n"
                          "def refresh():\n original(); result=original(); "
                          "s=open('/proc/self/status').read().splitlines(); "
                          "print('producer_rss_kb='+' '.join(next(x.split()[1] for x in s "
                          "if x.startswith(k+':')) for k in ('VmRSS','VmHWM')),flush=True); "
                          "return result\nh._refresh_cache=refresh\n"
                          "raise SystemExit(h.main(['--refresh-cache']))")
            child_state = root / "producer-measure-state"
            child_state.mkdir()
            child_env = dict(os.environ, XDG_STATE_HOME=str(child_state), HOME=str(root / "producer-home"),
                             AGENT_DISPATCH_JOBS=str(jobs), PATH=str(empty_bin),
                             PYTHONDONTWRITEBYTECODE="1")
            child_rss = subprocess.check_output(
                [sys.executable, "-c", child_code], env=child_env, text=True).strip().splitlines()[-1]
            rss_kb, hwm_kb = (int(value) for value in child_rss.split("=", 1)[1].split())
            print("case\tfixture_bytes\trows\troutes\tcandidates\tcurrent\twarm_p50_s\twarm_p95_s\twarm_max_s\tappend_s\tprocess_rss_mib\tprocess_hwm_mib")
            p95 = sorted(unchanged)[int(0.95 * (len(unchanged) - 1))]
            print(f"producer-warm\t{size}\t{len(rows)}\t1067\t380\t{result['total']}\t"
                  f"{statistics.median(unchanged):.6f}\t{p95:.6f}\t{max(unchanged):.6f}\t"
                  f"{append:.6f}\t{rss_kb / 1024:.2f}\t{hwm_kb / 1024:.2f}")
            print(f"registry-metrics\tcold_full_parses={after_cold['full_metadata_parses']}\t"
                  f"warm_full_parses={after_warm['full_metadata_parses']}\t"
                  f"append_delta_reads={after_append['delta_reads'] - after_warm['delta_reads']}\t"
                  f"append_delta_bytes={after_append['delta_bytes_read'] - after_warm['delta_bytes_read']}\t"
                  f"append_parsed_rows={after_append['metadata_rows_parsed'] - after_warm['metadata_rows_parsed']}")
            print("surface\tcase\tbaseline_p50_s\tbaseline_p95_s\tbaseline_max_s\traw_p50_s\traw_p95_s\traw_max_s\tdelta_p50_s\tdelta_p95_s\tdelta_max_s")
            deltas_all = []
            for case, surfaces in latency.items():
                for surface, (raw, baseline) in surfaces.items():
                    deltas = [a - b for a, b in zip(raw, baseline)]
                    deltas_all.extend(deltas)
                    def stats(values):
                        ordered = sorted(values)
                        return (statistics.median(ordered), ordered[int(0.95 * (len(ordered) - 1))],
                                max(ordered))
                    bstats, rstats, dstats = stats(baseline), stats(raw), stats(deltas)
                    print(f"{surface}\t{case}\t" + "\t".join(f"{v:.6f}" for group in
                          (bstats, rstats, dstats) for v in group))
            print(f"registry\tdetached_cold_pass_s={cold:.6f}\tparent_prime_s={parent_cold:.6f}\t"
                  f"bytes={size} rows={len(rows)} routes=1067 candidates=380 current={result['total']}")

            print(f"actual-producer-steady\tVmRSS_MiB={rss_kb / 1024:.2f}\tVmHWM_MiB={hwm_kb / 1024:.2f}")

            # Cold concurrent Claude statuslines exercise the real shell caller,
            # cache request helper, inherited flock and detached producer.
            concurrent = root / "concurrent"
            concurrent_state = concurrent / "state"
            config = concurrent / "claude-config"
            concurrent.mkdir()
            concurrent_state.mkdir()
            config.mkdir()
            concurrent_env = dict(os.environ, HOME=str(concurrent / "home"),
                XDG_STATE_HOME=str(concurrent_state), CLAUDE_CONFIG_DIR=str(config),
                AGENT_DISPATCH_JOBS=str(jobs), PYTHONDONTWRITEBYTECODE="1")
            for key in ("AGENT_HOME", "CLAUDE_HOME", "AGENT_SESSION_ROLE", "AGENT_DISPATCH_CHILD",
                        "AGENT_DISPATCH_DEPTH", "FLEET_TITLE_REFRESH", "MEM_DISTILL"):
                concurrent_env.pop(key, None)
            for key in tuple(concurrent_env):
                if key.startswith("OPENCODE_DISPATCH"):
                    concurrent_env.pop(key, None)
            statusline = ROOT / "adapters/claude/statusline.sh"
            payload = json.dumps({"cwd": str(root), "session_id": "sid-concurrent",
                                  "model": {"id": "fixture-model"}})
            barrier = threading.Barrier(16)
            outcomes = []
            def run_statusline():
                barrier.wait()
                completed = subprocess.run(["bash", str(statusline)], input=payload, text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=concurrent_env, timeout=30)
                outcomes.append(completed.returncode)
            threads = [threading.Thread(target=run_statusline) for _ in range(16)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=35)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(outcomes, [0] * 16)
            lock = concurrent_state / "hearting/human-waits/.projection.lock"
            producer_pids = set()
            until = time.monotonic() + 3
            while time.monotonic() < until:
                for entry in Path("/proc").iterdir():
                    if not entry.name.isdigit():
                        continue
                    try:
                        cmdline = (entry / "cmdline").read_bytes()
                        environ = (entry / "environ").read_bytes()
                    except OSError:
                        continue
                    if (b"human_wait_state.py\0--refresh-cache" in cmdline
                            and ("XDG_STATE_HOME=" + str(concurrent_state)).encode() in environ):
                        producer_pids.add(int(entry.name))
                if producer_pids:
                    break
                time.sleep(0.05)
            self.assertEqual(len(producer_pids), 1)
            lock_fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                import fcntl
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(lock_fd)
            print(f"concurrent-statusline\tcalls=16\tproducer_spawns={len(producer_pids)}\tlock_holders=1")
            for pid in producer_pids:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            cleanup_until = time.monotonic() + 2
            while time.monotonic() < cleanup_until:
                if all(not Path(f"/proc/{pid}/cmdline").exists()
                       or not Path(f"/proc/{pid}/cmdline").read_bytes() for pid in producer_pids):
                    break
                time.sleep(0.05)
            self.assertTrue(all(not Path(f"/proc/{pid}/cmdline").exists()
                                or not Path(f"/proc/{pid}/cmdline").read_bytes()
                                for pid in producer_pids))
            self.assertLessEqual(max(unchanged), 1.0)
            self.assertLessEqual(append, 1.0)
            self.assertLessEqual(max(deltas_all), 0.2)
            self.assertLessEqual(rss_kb / 1024, 200)


if __name__ == "__main__":
    unittest.main()
