#!/usr/bin/env python3
"""Replacement, release-pinning and re-exec starts through real, isolated wrappers.

Split from execution_access_owner_start.test.py, which keeps the shared fixture,
so the two halves run on separate CI shards. Substitutions are the same: the
model payload, runtime bootstrap/observations and frame readiness are replaced;
route/request validation, registration, fencing, spawning and grant I/O are real.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


FIXTURE = load_module("owner_grant_start_fixture", HERE / "execution_access_owner_start.test.py")


class OwnerGrantReplacementStartTest(FIXTURE.OwnerGrantStartFixture):
    def test_two_blocked_corrections_spawn_the_exact_final_replacement_once_on_each_adapter(self):
        import dispatch_owner_input as owner_input
        import dispatch_replacement as replacement

        self.env["CLAUDE_SESSION_ID"] = "fixture-parent-session"
        self.worker.write_text(
            "import os,pathlib,sys,time\n"
            "marker=pathlib.Path(sys.argv[1]); release=pathlib.Path(sys.argv[2])\n"
            "with (marker.parent/'spawn-ledger').open('a') as ledger:\n"
            " ledger.write(marker.name.removesuffix('.started')+'\\t'+str(os.getpid())+'\\n')\n"
            "marker.write_text(str(os.getpid()))\n"
            "while not release.exists(): time.sleep(.01)\n")

        for adapter in ("codex", "claude", "opencode"):
            with self.subTest(adapter=adapter):
                route = self.route(adapter)
                external_read = self.root / (adapter + "-external-read")
                external_read.mkdir()
                request = self.request(adapter + "-lineage", read_roots=(external_read,))
                origin = "att-" + hashlib.sha256((adapter + "-blocked-origin").encode()).hexdigest()[:32]
                corrections = {}
                starts = []

                first_start, first_output = self.start(
                    adapter, origin, request, route=route, capture_native=True)
                self.assertEqual(first_start, 0, first_output)
                self.assertIn("started=1", first_output)
                _origin_fields, origin_row = self.row(origin)
                origin_pid = (self.root / (origin + ".started")).read_text()
                self.assertTrue(origin_row["pid"])
                self.assertEqual(sum(line.startswith(origin + "\t") for line in
                                     (self.root / "spawn-ledger").read_text().splitlines()), 1)
                self.assertIn(f"{origin}\t{origin_pid}",
                              (self.root / "spawn-ledger").read_text().splitlines())
                signatures = [self._assert_replacement_launch_evidence(adapter, origin, external_read)]

                parent_env = dict(self.env)
                def launch_replacement(command, *, env=None, **_kwargs):
                    self.assertEqual(Path(command[1]), ROOT / "utilities/dispatch-owner.py")
                    def option(name):
                        return command[command.index(name) + 1]
                    predecessor = option("--automatic-retry-of")
                    attempt = option("--attempt-id")
                    prompt_file = Path(option("--prompt-file"))
                    self.assertEqual(Path(option("--route-evidence")), route[0])
                    self.assertEqual(Path(option("--jobs")), self.jobs)
                    self.assertEqual(Path(option("--execution-access-file")), request)
                    result, output = self.start(
                        adapter, attempt, request, route=route,
                        automatic_retry_of=predecessor, prompt_file=prompt_file,
                        runtime_env=env, slug=option("--slug"), capture_native=True)
                    self.assertEqual(result, 0, output)
                    self.assertIn("started=1", output)
                    rendered_prompt = next(
                        Path(line.split("=", 1)[1]) for line in output.splitlines()
                        if line.startswith("prompt_file=")
                    )
                    self.assertIn(corrections[predecessor],
                                  rendered_prompt.read_text(encoding="utf-8"))
                    _fields, row = self.row(attempt)
                    self.assertEqual(row["automatic_retry_of"], predecessor)
                    self.assertEqual(row["harness"], adapter)
                    self.assertTrue(row["pid"])
                    ledger_rows = (self.root / "spawn-ledger").read_text().splitlines()
                    self.assertIn(f"{attempt}\t{(self.root / (attempt + '.started')).read_text()}",
                                  ledger_rows)
                    starts.append((attempt, predecessor, prompt_file))
                    signatures.append(self._assert_replacement_launch_evidence(
                        adapter, attempt, external_read))
                    return subprocess.CompletedProcess(command, result, output, "")

                def advance(attempt):
                    with mock.patch.dict(os.environ, parent_env, clear=True):
                        with mock.patch("dispatch_capacity_evidence.harness_hold", return_value=None):
                            with mock.patch.object(replacement, "_reuse_snapshot", return_value={
                                    "completed": [], "cycle_id": "cyc-fixture",
                                    "producer_id": "prod-fixture", "gate_releases": []}):
                                return replacement.advance(
                                    self.jobs, attempt, run=launch_replacement, resume_capacity=True)

                self._end_blocked_and_correct(
                    adapter, origin, "approved: start full run", "answer-origin")
                corrections[origin] = "approved: start full run"
                first_result = advance(origin)
                self.assertEqual(len(starts), 1, first_result)
                self.assertIn("record", first_result, first_result)
                first_record = first_result["record"]
                first = first_record["replacement_attempt_id"]
                self.assertEqual(first_result["attempt_id"], first, first_result)
                self.assertEqual(first_record["original_attempt_id"], origin)
                self.assertEqual(first_record["proof"]["death_kind"], replacement.CORRECTED)
                self.assertEqual(len(starts), 1)
                self._end_blocked_and_correct(
                    adapter, first, "approved: continue phase two", "answer-first")
                corrections[first] = "approved: continue phase two"
                second_result = advance(first)
                self.assertEqual(len(starts), 2, second_result)
                self.assertIn("record", second_result, second_result)
                second_record = second_result["record"]
                second = second_record["replacement_attempt_id"]
                self.assertEqual(second_result["attempt_id"], second, second_result)
                self.assertEqual(second_record["original_attempt_id"], first)
                self.assertEqual(second_record["proof"]["death_kind"], replacement.CORRECTED)
                self.assertEqual(len(starts), 2)
                self.assertEqual(starts[0][1], origin)
                self.assertEqual(starts[1][1], first)

                for predecessor, record in ((origin, first_record), (first, second_record)):
                    _fields, source = self.row(predecessor)
                    self.assertEqual(replacement.source_binding(self.jobs, source),
                                     (record["family_id"], record["replacement_attempt_id"],
                                      replacement._digest(record)))
                    _child_fields, child = self.row(record["replacement_attempt_id"])
                    self.assertEqual(child["replacement_original_attempt_id"], predecessor)
                    self.assertEqual(child["replacement_family_id"], record["family_id"])
                    self.assertEqual(child["replacement_claim_digest"], replacement._digest(record))
                    self.assertEqual(child["automatic_retry_of"], predecessor)
                    self.assertEqual(replacement.claim(self.jobs, predecessor), record)

                effective, lineage = replacement.effective_attempts(self.jobs, {origin})
                self.assertEqual(effective, {second})
                self.assertEqual(len(lineage), 1)
                self.assertEqual(lineage[0]["original_attempt_id"], origin)
                self.assertEqual(lineage[0]["replacement_attempt_id"], second)
                self.assertEqual(lineage[0]["family_id"], first_record["family_id"])
                self.assertEqual(signatures[0], signatures[1])
                self.assertEqual(signatures[1], signatures[2])

                ledger = self.root / "spawn-ledger"
                before_duplicate = ledger.read_text(encoding="utf-8").splitlines()
                counts = {attempt: sum(line.split("\t", 1)[0] == attempt
                                       for line in before_duplicate)
                          for attempt in (origin, first, second)}
                self.assertEqual(counts, {origin: 1, first: 1, second: 1})
                final_pid = (self.root / (second + ".started")).read_text()
                self.assertTrue(self.row(second)[1]["pid"])
                self.assertIn(f"{second}\t{final_pid}", before_duplicate)

                calls_before = len(starts)
                replayed = advance(origin)
                self.assertIn(replayed["state"], {"running", "reused"}, replayed)
                self.assertEqual(replayed["attempt_id"], second)
                self.assertEqual(len(starts), calls_before)
                with mock.patch.object(replacement, "_reuse_snapshot", return_value={
                        "completed": [], "cycle_id": "cyc-fixture",
                        "producer_id": "prod-fixture", "gate_releases": []}):
                    duplicate, duplicate_output = self.start(
                        adapter, second, request, route=route, automatic_retry_of=first,
                        prompt_file=starts[1][2], capture_native=True)
                self.assertNotEqual(duplicate, 0, duplicate_output)
                self.assertIn("child_spawned=0", duplicate_output)
                self.assertEqual(ledger.read_text(encoding="utf-8").splitlines(), before_duplicate)
                self.assertEqual((self.root / (second + ".started")).read_text(), final_pid)
                after_duplicate = replacement.effective_attempts(self.jobs, {origin})
                self.assertEqual(after_duplicate, (effective, lineage))

    def test_each_owner_tree_runs_on_the_release_its_launch_resolved(self):
        # Launched through a moving pointer (`<share>/hearting/current`), the owner gets the
        # release the pointer named at launch, as the row records it (OPERATIONS §5.9a), so a
        # later install moves neither. Before, the child inherited the pointer itself.
        pointer = self.root / "current"
        pointer.symlink_to(ROOT)
        self.env["AGENT_HOME"] = str(pointer)
        self.worker.write_text(
            "import os,pathlib,sys,time\n"
            "pathlib.Path(sys.argv[1] + '.home').write_text(os.environ.get('AGENT_HOME', ''))\n"
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
            "while not pathlib.Path(sys.argv[2]).exists(): time.sleep(.01)\n")
        wrapper_common = self.wrappers["claude"].WRAPPER_COMMON
        with mock.patch.object(
            wrapper_common, "select_execution_root",
            wraps=wrapper_common.select_execution_root,
        ) as reexec:
            for adapter in ("claude", "codex", "opencode"):
                with self.subTest(adapter=adapter):
                    attempt = "att-" + hashlib.sha256(("pin-" + adapter).encode()).hexdigest()[:32]
                    result, output = self.start(adapter, attempt, None, route=self.route(adapter))
                    self.assertEqual(0, result, output)
                    _, row = self.row(attempt)
                    self.assertEqual(str(ROOT.resolve()), row["launch_home"])
                    self.assertEqual(str(ROOT.resolve()), (self.root / (attempt + ".started.home")).read_text())
        self.assertEqual(reexec.call_count, 3)
        self.assertEqual([call.args[2] for call in reexec.call_args_list],
                         ["claude", "codex", "opencode"])
        self.assertTrue(all(Path(call.args[0]).resolve() == ROOT.resolve()
                            and Path(call.args[1]).resolve() == ROOT.resolve()
                            and call.args[3][0] == "--start"
                            for call in reexec.call_args_list))

    def test_sibling_release_reexec_keeps_one_registration_and_pins_child_home(self):
        releases = self.root / "managed" / "releases"
        releases.mkdir(parents=True)
        release_a, release_b = releases / "vA", releases / "vB"
        self.make_release_fixture(release_a)
        self.make_release_fixture(release_b)
        self.worker.write_text(
            "import os,pathlib,sys,time\n"
            "pathlib.Path(sys.argv[1] + '.home').write_text(os.environ.get('AGENT_HOME', ''))\n"
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
            "while not pathlib.Path(sys.argv[2]).exists(): time.sleep(.01)\n")
        self.env["AGENT_HOME"] = str(ROOT)

        for adapter in ("claude", "codex", "opencode"):
            for source, sealed in ((release_a, release_b), (release_b, release_a)):
                with self.subTest(adapter=adapter, source=source.name, sealed=sealed.name):
                    route = self.route(adapter)
                    self.env["AGENT_HOME"] = str(sealed)
                    attempt = "att-" + hashlib.sha256(
                        (adapter + source.name + sealed.name).encode()).hexdigest()[:32]
                    result, output = self.start(
                        adapter, attempt, None, route=route,
                        release_pair=(source, sealed),
                    )
                    self.assertEqual(result, 0, output)
                    self.assertIn("started=1", output)
                    _fields, row = self.row(attempt)
                    self.assertEqual(row["launch_home"], str(sealed.resolve()))
                    self.assertEqual((self.root / (attempt + ".started.home")).read_text(),
                                     str(sealed.resolve()))
                    self.assertEqual(len(self.last_exec_calls), 1)
                    executable, vector = self.last_exec_calls[0]
                    self.assertEqual(executable, sys.executable)
                    self.assertEqual(Path(vector[1]).resolve(),
                                     (sealed / "adapters" / adapter / "bin" /
                                      "dispatch-headless.py").resolve())
                    registered = [line for line in self.jobs.read_text(encoding="utf-8").splitlines()
                                  if f"attempt_id={attempt}" in line]
                    self.assertEqual(len(registered), 1)

    def test_installed_release_is_the_one_root_of_guard_row_and_child_when_the_tree_inherited_an_older_home(self):
        # T4: the wrapper runs the installed release B, the tree inherited A. Without any
        # re-exec, the row's launch_home and the child's AGENT_HOME are B, the root that ran admission.
        import peer_obligations
        releases = self.root / "managed" / "releases"
        releases.mkdir(parents=True)
        release_a, release_b = releases / "vA", releases / "vB"
        self.make_release_fixture(release_a)
        self.make_release_fixture(release_b)
        self.worker.write_text(
            "import os,pathlib,sys,time\n"
            "pathlib.Path(sys.argv[1] + '.home').write_text(os.environ.get('AGENT_HOME', ''))\n"
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
            "while not pathlib.Path(sys.argv[2]).exists(): time.sleep(.01)\n")
        for adapter in ("claude", "codex", "opencode"):
            with self.subTest(adapter=adapter):
                self.env["AGENT_HOME"] = str(release_a)
                attempt = "att-" + hashlib.sha256(("installed-" + adapter).encode()).hexdigest()[:32]
                with mock.patch.object(peer_obligations, "installed_release",
                                       return_value=release_b.resolve()):
                    result, output = self.start(adapter, attempt, None, route=self.route(adapter),
                                                release_pair=(release_b, release_a))
                self.assertEqual(result, 0, output)
                self.assertEqual(self.last_exec_calls, [])
                _fields, row = self.row(attempt)
                self.assertEqual(row["launch_home"], str(release_b.resolve()))
                self.assertEqual((self.root / (attempt + ".started.home")).read_text(),
                                 str(release_b.resolve()))


if __name__ == "__main__":
    unittest.main()
