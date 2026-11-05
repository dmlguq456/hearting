#!/usr/bin/env python3
"""HOOK-LATENCY 5-2 #8: one harness status reads the dispatch registry once.

OpenCode runs `harness-status.sh` (through `preflight.sh prompt-signal`) on
every user turn. It used to read jobs.log four times in four processes: two
awk passes for the open rows, `observed-status` and `orphan-scan`. The facts
stay the same; the reads are counted, not timed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
STATUS = ROOT / "utilities" / "harness-status.sh"
REGISTRY = ROOT / "utilities" / "dispatch-registry.py"

# Logs every open of the watched file by any Python process of the run.
SITECUSTOMIZE = textwrap.dedent("""
    import os, sys
    _watched = os.environ.get("REGISTRY_READS_WATCH")
    _log = os.environ.get("REGISTRY_READS_LOG")
    if _watched and _log:
        _real = os.path.realpath(_watched)
        def _audit(event, args):
            if event != "open" or not args or isinstance(args[0], int):
                return
            try:
                if os.path.realpath(os.fsdecode(args[0])) == _real:
                    with open(_log, "a") as handle:
                        handle.write(os.path.basename(sys.argv[0] or "-") + "\\n")
            except Exception:
                pass
        sys.addaudithook(_audit)
""")


class HarnessStatusRegistryReadsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="status-reads-")
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.jobs = base / "dispatch" / "jobs.log"
        self.jobs.parent.mkdir()
        # Rows name no live process: attempt ids are fresh and pids absent.
        def row(status, slug, **meta):
            pipe = ",".join(f"{key}={value}" for key, value in
                            {"attempt_id": "att-" + uuid.uuid4().hex, **meta}.items())
            return "\t".join(("2026-11-04T00:00:00Z", status, "/r", "/w", slug, pipe))
        self.jobs.write_text("\n".join((
            row("open", "job-a"),
            row("running", "job-run"),
            row("open", "job-b", worker_type="owner"),
            row("done", "job-done"),
            "malformed\topen\trow",
        )) + "\n", encoding="utf-8")
        site = base / "site"
        site.mkdir()
        (site / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
        tools = base / "bin"
        tools.mkdir()
        self.awk_log = base / "awk.log"
        real_awk = shutil.which("awk")
        (tools / "awk").write_text(
            "#!/bin/sh\n"
            f"for arg in \"$@\"; do [ \"$arg\" = {self.jobs} ] && echo jobs >> {self.awk_log}; done\n"
            f"exec {real_awk} \"$@\"\n", encoding="utf-8")
        (tools / "awk").chmod(0o755)
        self.reads = base / "reads.log"
        self.env = {
            **os.environ,
            "HOME": str(base / "home"),
            "XDG_STATE_HOME": str(base / "state"),
            "AGENT_DISPATCH_JOBS": str(self.jobs),
            "AGENT_RESOURCE_RUN_INDEX": str(base / "resource-index.json"),
            "PYTHONPATH": os.pathsep.join(filter(None, (str(site), os.environ.get("PYTHONPATH")))),
            "PATH": str(tools) + os.pathsep + os.environ.get("PATH", ""),
            "REGISTRY_READS_WATCH": str(self.jobs),
            "REGISTRY_READS_LOG": str(self.reads),
        }

    def _lines(self, path):
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def test_one_status_reads_the_registry_once_and_reports_the_same_facts(self):
        result = subprocess.run([str(STATUS), str(ROOT), "test"], env=self.env,
                                text=True, capture_output=True, timeout=120, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        # Base: two awk passes plus observed-status and orphan-scan, each reading it.
        self.assertEqual(self._lines(self.awk_log), [])
        self.assertEqual(self._lines(self.reads), ["dispatch-registry.py"])
        facts = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        self.assertEqual(facts["headless_open_jobs"], "2")
        self.assertEqual(facts["headless_open_slugs"], "job-a,job-b")
        self.assertEqual(facts["orphaned_conductor_jobs"], "0")
        self.assertNotIn("orphaned_resume_boundary", facts)

        # The same facts the former separate commands print for this registry.
        env = {key: value for key, value in self.env.items()
               if key not in {"REGISTRY_READS_WATCH", "REGISTRY_READS_LOG"}}
        separate = {}
        for operation in ("observed-status", "orphan-scan"):
            done = subprocess.run([sys.executable, str(REGISTRY), operation, "--jobs", str(self.jobs),
                                   "--agent-home", str(ROOT)], env=env, text=True,
                                  capture_output=True, timeout=120, check=False)
            self.assertEqual(done.returncode, 0, done.stderr)
            separate.update(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)
        self.assertEqual(json.loads(facts["headless_evidence"]),
                         json.loads(separate["headless_evidence"]))
        self.assertEqual(facts["orphaned_conductor_jobs"], separate["orphaned_conductor_jobs"])

    def test_the_prompt_signal_reads_only_the_open_count(self):
        # The per-prompt signal prints git, artifact-root and open-row facts and
        # nothing from process evidence, orphan scan or resource counts; those
        # are computed when the full status is asked for (HOOK2 follow-up).
        # The git and artifact-root fields come from the same shared reader as
        # the full status (its cache warms asynchronously, so values are not
        # compared here); the registry part is what changes.
        env = {**self.env, "AGENT_HOME": str(ROOT)}
        for adapter, fields in (
                ("opencode", ("artifact_root_kind", "git_operation", "headless_open_jobs")),
                ("codex", ("artifact_root_kind", "git_operation", "git_dirty_tracked",
                           "git_untracked", "git_extra_worktrees", "git_branch_done",
                           "headless_open_jobs"))):
            with self.subTest(adapter=adapter):
                for log in (self.reads, self.awk_log):
                    log.unlink(missing_ok=True)
                signal = subprocess.run(
                    [str(ROOT / "adapters" / adapter / "bin" / "preflight.sh"), "prompt-signal",
                     str(ROOT), "test"], env=env, text=True,
                    capture_output=True, timeout=120, check=False)
                self.assertEqual(signal.returncode, 0, signal.stderr)
                facts = dict(line.split("=", 1) for line in signal.stdout.splitlines() if "=" in line)
                self.assertLessEqual(set(fields), set(facts))
                self.assertEqual(facts["headless_open_jobs"], "2")
                # Base: the full status ran on every prompt (one Python read).
                self.assertEqual(self._lines(self.reads), [])
                self.assertEqual(self._lines(self.awk_log), ["jobs"])

    def test_a_failed_shared_read_keeps_the_count_and_reports_unknown_evidence(self):
        broken = Path(self._tmp.name) / "broken-home"
        (broken / "utilities").mkdir(parents=True)
        for name in ("harness-status.sh", "dispatch-state-root.sh"):
            shutil.copy2(ROOT / "utilities" / name, broken / "utilities" / name)
        (broken / "utilities" / "dispatch-registry.py").write_text("raise SystemExit(70)\n")
        result = subprocess.run([str(broken / "utilities" / "harness-status.sh"), str(ROOT), "test"],
                                env=self.env, text=True, capture_output=True, timeout=120,
                                check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("headless_open_jobs=2\nheadless_open_slugs=job-a,job-b\n"
                      "headless_evidence=unknown\norphaned_conductor_jobs=0\n", result.stdout)


if __name__ == "__main__":
    unittest.main()
