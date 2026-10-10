#!/usr/bin/env python3
"""The runtime closes routes nobody works on any more (route_autoclose.py).

Every case drives the public CLI (`compose`, `start`, `status`,
`campaign-status`, `campaign-close`) in an isolated artifact root, route-chain
ledger, dispatch registry, workflow ledger, resource-run index and Claude
session registry.  The R- and N-cases are the PR #53 review reproductions
(rounds 1 and 2), kept as regressions.
"""
from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import artifact_producer  # noqa: E402

CAP = ROOT / "utilities/capability-route.py"
PRODUCER = ROOT / "utilities/artifact_producer.py"
SESSION_VARS = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID",
                "OPENCODE_SESSION_ID", "AGENT_DISPATCH_CALLER_HARNESS", "AGENT_DISPATCH_CURRENT_HARNESS",
                "AGENT_DISPATCH_ATTEMPT_ID", "AGENT_DISPATCH_PARENT_SESSION_ID", "AGENT_ROUTE_FILE",
                "AGENT_ROUTE_ID", "AGENT_ROUTE_NODE", "HEARTING_INLINE_FINISH_CRASH_AT",
                "AGENT_WORKFLOW_ROOT", "AGENT_RESOURCE_RUN_INDEX")
SESSION_ENV = {"claude": "CLAUDE_CODE_SESSION_ID", "codex": "CODEX_THREAD_ID", "opencode": "OPENCODE_SESSION_ID"}
TWO_HOURS, TWO_DAYS, EIGHT_DAYS = 2 * 3600, 2 * 24 * 3600, 8 * 24 * 3600


# Only these reach the commands under test; everything else is set explicitly,
# so a caller running inside a dispatch worker (AGENT_ARTIFACT_*, AGENT_WORKFLOW_ROOT,
# session ids, XDG state) cannot steer a test write into a real artifact root.
INHERITED = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "TMPDIR", "USER", "LOGNAME", "SHELL",
             "GIT_CONFIG_GLOBAL")  # Preserve the official runner's private safe.directory.


def isolated_env(**explicit) -> dict:
    # The group-review and campaign-title switches are pinned last: a sealed cycle would otherwise start
    # a detached sweep that keeps writing into the fixture root while teardown
    # removes it (the runner sets the switches, but INHERITED drops them).
    return {**{key: os.environ[key] for key in INHERITED if key in os.environ}, **explicit,
            "HEARTING_WORKFLOW_GROUP_REVIEW": "off", "HEARTING_CAMPAIGN_TITLE_AUTO": "off"}


def _proc_start(pid: int) -> str:
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


class RouteAutocloseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="route-autoclose-test-")
        self.base = base = Path(self.temp.name)
        self.repo, self.root = base / "repo", base / "artifacts"
        self.repo.mkdir(); self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.email=t@example.com", "-c", "user.name=t",
                        "commit", "-qm", "base"], check=True)
        self.jobs = base / "state" / "dispatch" / "jobs.log"
        self.jobs.parent.mkdir(parents=True)
        self.jobs.write_text("", encoding="utf-8")
        self.sessions = base / "claude" / "sessions"
        self.sessions.mkdir(parents=True)
        self.ledgers = base / "route-chains"
        self.resource_index = base / "resource-runs.index.json"
        self.env = isolated_env(
            AGENT_HOME=str(ROOT), AGENT_DISPATCH_JOBS=str(self.jobs), AGENT_DISPATCH_DEPTH="0",
            XDG_STATE_HOME=str(base / "xdg"), FLEET_ROUTE_CHAIN_DIR=str(self.ledgers),
            CLAUDE_CONFIG_DIR=str(base / "claude"), AGENT_RESOURCE_RUN_INDEX=str(self.resource_index))
        artifact_producer.activate(self.root, repository_id="repo_" + "a" * 32,
                                   artifact_root_id="root_" + "b" * 32,
                                   w7={"campaign_id": "camp_" + "c" * 32})
        self.prompt = base / "task.md"
        self.prompt.write_text("Tighten the report generator.\n", encoding="utf-8")
        # Two supported headless peers, the fixture artifact_producer.test.py uses:
        # an isolated test host has no runtime to probe.
        probe = {"transport": "headless", "surface": "registered-headless", "status": "supported",
                 "probe_source": "fixture-probe", "probe_time": "2026-07-20T00:00:00Z"}
        self.headless = base / "headless.json"
        self.headless.write_text(json.dumps({"candidates": [{**probe, "harness": "codex"},
                                                            {**probe, "harness": "claude"}]}))
        self.processes = []
        self.sweeps = 0

    def tearDown(self):
        for proc in self.processes:
            proc.kill(); proc.wait()
        self.temp.cleanup()

    # ---- drivers --------------------------------------------------------
    def run_as(self, harness, sid, *argv, program=CAP):
        env = dict(self.env)
        if harness:
            env[SESSION_ENV[harness]] = sid
        return subprocess.run([sys.executable, str(program), *map(str, argv)], cwd=self.repo, env=env,
                              capture_output=True, text=True)

    def compose(self, slug, harness="codex", sid="session-1", *, intensity="direct", campaign="k1", start=True):
        shape = "direct" if intensity == "direct" else "solo"
        argv = ["compose", "--slug", slug, "--campaign-key", campaign, "--shape", shape,
                "--capability", "autopilot-code", "--capability-mode", "dev", "--intensity", intensity,
                "--cwd", self.repo, "--artifact-root", self.root, "--tracking", "tracked",
                "--prompt-file", self.prompt, "--spec-read", "fixture", "--drift-verdict", "within-spec",
                "--artifact-guard", "fixture"]
        if intensity == "direct":
            argv += ["--owner", harness, "--parent-harness", harness]
        else:
            argv += ["--registered-headless-evidence", self.headless]
        done = self.run_as(harness, sid, *argv)
        self.assertEqual(done.returncode, 0, done.stderr)
        route_file = Path(json.loads(done.stdout)["route_file"])
        if start:
            started = self.run_as(harness, sid, "start", "--route", route_file, "--jobs", self.jobs)
            self.assertEqual(started.returncode, 0, started.stderr)
        self.last_stderr = done.stderr
        return route_file, json.loads(route_file.read_text(encoding="utf-8"))

    def later(self):
        """RECHECK_SECONDS pass: routes and cycles the evidence kept are judged again."""
        state = self.root / ".runtime/route-autoclose/state.json"
        if state.is_file():
            data = json.loads(state.read_text())
            data["kept"] = {}
            state.write_text(json.dumps(data))

    def sweep(self):
        """The next compose from some other session is what triggers a sweep.  A compose sweeps its own
        campaign, so that campaign must exist: a started route in `k1` stands in when none does."""
        self.sweeps += 1
        if artifact_producer.find_campaign_by_key(self.root, "k1") is None:
            self.compose("campaign-anchor", "codex", "anchor")
        self.compose(f"observer-{self.sweeps}", "codex", "observer", start=False)
        return self.last_stderr

    def status(self):
        done = self.run_as("codex", "observer", "status", "--artifact-root", self.root, "--open-only")
        self.assertEqual(done.returncode, 0, done.stderr)
        return {row["route_id"] for row in json.loads(done.stdout)}

    def campaign(self, verb, campaign, *extra):
        return self.run_as("codex", "observer", verb, "--artifact-root", self.root, "--campaign", campaign,
                           *extra, program=PRODUCER)

    def cycle(self, route):
        return artifact_producer.route_cycle_for(self.root, route)

    def cycle_record(self, cycle_id):
        return artifact_producer.read_cycle_record(self.root, cycle_id)

    def cycle_dir(self, record):
        return artifact_producer.cycle_dir(self.root, record["campaign_id"], record["cycle_id"], record)

    def write_artifact(self, route, name="report.md", body="the report\n"):
        record = self.cycle(route)
        target = self.cycle_dir(record) / "artifacts" / "documents" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        return record

    def outcome(self, route_file):
        path = route_file.with_name(route_file.stem + ".outcome.json")
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def age(self, route_file, harness, sid, seconds=EIGHT_DAYS):
        """`seconds` of nothing: route, cycles and the composer's own ledger."""
        old = time.time() - seconds
        ledger = self.ledgers / harness / f"{sid}.jsonl"
        if ledger.is_file():
            lines = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
            ledger.write_text("".join(json.dumps({**line, "ts": old}) + "\n" for line in lines))
            os.utime(ledger, (old, old))
        for path in [route_file, *self.root.joinpath(".runtime/artifact-producer/v1/cycles").glob("*.json"),
                     *self.root.joinpath("campaigns").rglob("*")]:
            os.utime(path, (old, old))

    def spawn(self, *argv):
        proc = subprocess.Popen([sys.executable, "-c", "import sys, time; time.sleep(300)", *argv])
        self.processes.append(proc)
        for _ in range(100):   # until exec replaced the forked parent's argv
            if b"time.sleep(300)" in Path(f"/proc/{proc.pid}/cmdline").read_bytes():
                break
            time.sleep(0.02)
        return proc

    def claude_record(self, proc, sid):
        (self.sessions / f"{proc.pid}.json").write_text(json.dumps(
            {"pid": proc.pid, "sessionId": sid, "procStart": _proc_start(proc.pid)}))

    def claude_alive(self, sid):
        proc = self.spawn()
        self.claude_record(proc, sid)
        return proc

    def claude_crashed(self, sid):
        """A Claude session that died without removing its registry record."""
        proc = self.spawn()
        self.claude_record(proc, sid)
        proc.kill(); proc.wait()

    def raise_gate(self, route):
        """Journal BLOCKED_HUMAN_GATE the way `workflow-supervisor.py gate --block` does."""
        import workflow_state
        gate = (route.get("human_gate_bindings") or [{}])[0].get("gate") or "plan-approval"
        ledger = workflow_state.WorkflowLedger(route["route_id"], route["route_hash"], jobs=str(self.jobs))
        with ledger.lock():
            for state in ("READY", "RUNNING"):
                ledger.set_workflow_state(state, actor="test")
            ledger.set_workflow_state("BLOCKED_HUMAN_GATE", evidence={"gate": gate, "artifact": "/x/q.md"},
                                      actor="test")
        self.assertEqual(workflow_state.human_gate_resolution(ledger.journal(), gate)["status"], "blocked")

    # ---- closes, claiming no proof -----------------------------------------
    def test_idle_direct_closes_without_claiming_proof_and_abandons_its_cycle(self):
        route_file, route = self.compose("quiet", "opencode", "ses-oc")
        record = self.write_artifact(route)
        self.age(route_file, "opencode", "ses-oc", TWO_DAYS)
        self.sweep()
        self.assertIsNone(self.outcome(route_file))   # a week of quiet first
        self.age(route_file, "opencode", "ses-oc")
        self.assertIn("route_autoclose closed=1", self.sweep())
        outcome = self.outcome(route_file)
        self.assertEqual((outcome["autoclose"]["reason"], outcome["autoclose"]["proof"]), ("idle", "not-claimed"))
        self.assertIs(outcome["terminal_gate_proven"], False)
        self.assertNotIn("inline_finish_id", outcome)
        sealed = self.cycle_record(record["cycle_id"])
        self.assertEqual((sealed["state"], sealed["cycle_state"]), ("sealed", "abandoned"))

    def test_idle_direct_without_output_drops_its_empty_cycle(self):
        route_file, route = self.compose("empty", "codex", "session-9")
        record = self.cycle(route)
        self.age(route_file, "codex", "session-9")
        self.sweep()
        self.assertIs(self.outcome(route_file)["terminal_gate_proven"], False)
        self.assertEqual(self.cycle_record(record["cycle_id"])["state"], "abandoned")

    def test_crashed_claude_session_closes_after_an_hour_of_quiet(self):
        self.claude_crashed("claude-crashed")
        route_file, route = self.compose("orphan", "claude", "claude-crashed")
        self.sweep()
        self.assertIsNone(self.outcome(route_file))
        self.age(route_file, "claude", "claude-crashed", TWO_HOURS)
        self.sweep()
        self.assertEqual(self.outcome(route_file)["autoclose"]["reason"], "session-ended")

    def test_quick_route_without_owner_closes_with_its_honest_proof(self):
        self.claude_crashed("claude-crashed")
        route_file, route = self.compose("solo", "claude", "claude-crashed", intensity="quick", start=False)
        self.assertEqual(route["effective_intensity"], "quick")
        self.age(route_file, "claude", "claude-crashed", TWO_HOURS)
        self.sweep()
        outcome = self.outcome(route_file)
        self.assertEqual(outcome["autoclose"]["reason"], "session-ended")
        self.assertIs(outcome["terminal_gate_proven"], False)
        self.assertTrue(outcome["terminal_gates"])

    def test_cycle_left_open_after_its_route_closed_is_sealed(self):
        route_file, route = self.compose("closed-by-hand", "codex", "session-1")
        record = self.write_artifact(route)
        closed = self.run_as("codex", "session-1", "close", "--route", route_file, "--allow-unproven")
        self.assertEqual(closed.returncode, 0, closed.stderr)
        self.sweep()
        self.assertEqual(self.cycle_record(record["cycle_id"])["state"], "open")   # closed just now
        old = time.time() - TWO_HOURS
        os.utime(route_file.with_name(route_file.stem + ".outcome.json"), (old, old))
        self.sweep()
        sealed = self.cycle_record(record["cycle_id"])
        self.assertEqual((sealed["state"], sealed["cycle_state"]), ("sealed", "abandoned"))

    def test_a_big_cycle_behind_a_closed_route_is_sealed_over_several_sweeps_and_no_sweep_waits_for_it(self):
        import artifact_admission
        import route_autoclose as RA
        route_file, route = self.compose("big-closed", "codex", "session-1")
        record = self.write_artifact(route)
        scratch = self.cycle_dir(record) / "artifacts" / "plans" / "evidence" / "scratch" / "pytest-all"
        for index in range(40):
            target = scratch / f"test_k{index}0" / "out.json"
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps({"case": index}) * 3, encoding="utf-8")
        closed = self.run_as("codex", "session-1", "close", "--route", route_file, "--allow-unproven")
        self.assertEqual(closed.returncode, 0, closed.stderr)
        old = time.time() - TWO_HOURS
        os.utime(route_file.with_name(route_file.stem + ".outcome.json"), (old, old))
        found = RA.campaign_of_route(self.root, route)
        api = self._api()
        real_monotonic, offset = time.monotonic, [0.0]
        real_facts = artifact_producer._stream_file_facts
        reads, held = [], []

        def facts(path):
            reads.append(str(path))
            held.append(artifact_admission.holds_lock(self.root))
            offset[0] += 1.0   # one second of slow storage per file
            return real_facts(path)

        def one_sweep():
            before = len(reads)
            with mock.patch.dict(os.environ, self.env):
                summary = RA.sweep(self.root, api=api, trigger="compose", scope_campaign_id=found[0],
                                   scope_dir=found[1], scope_key=RA.campaign_key_of(route), budget=5.0)
            self.assertEqual(summary["errors"], [])
            self.assertLessEqual(len(reads) - before, 6)    # one sweep never reads past its budget
            return summary

        cycle_id = record["cycle_id"]
        with mock.patch.object(time, "monotonic", lambda: real_monotonic() + offset[0]), \
                mock.patch.object(artifact_producer, "_stream_file_facts", facts):
            first = one_sweep()
            self.assertEqual([row["cycle"] for row in first["cycles"]], ["cycle-left-open:scan-in-progress"])
            self.assertEqual(self.cycle_record(cycle_id)["state"], "open")
            self.assertFalse(any(held))                     # nothing was read under the admission lock
            # A file nobody read yet goes away between two sweeps: the manifest must not list it.
            payload = sorted(scratch.glob("*/out.json"))
            gone = next(path for path in reversed(payload) if str(path) not in reads)
            gone.unlink()
            for _ in range(12):
                if self.cycle_record(cycle_id)["state"] == "sealed":
                    break
                one_sweep()
        self.assertEqual(self.cycle_record(cycle_id)["state"], "sealed")
        self.assertFalse(any(held))
        self.assertEqual(len(reads), len(set(reads)))       # every file was read once, over all sweeps
        directory = self.cycle_dir(self.cycle_record(cycle_id))
        document = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        listed = {row["locator"]["path"]: row for row in document["artifact_revisions"]}
        self.assertEqual(len(listed), 40)                   # 41 files, one deleted
        self.assertEqual(len(reads), 40)
        self.assertNotIn("artifacts/plans/evidence/scratch/pytest-all/" + gone.parent.name + "/out.json", listed)
        for rel, row in listed.items():
            self.assertEqual(row["content_digest"], "sha256:" + hashlib.sha256((directory / rel).read_bytes()).hexdigest(), rel)
        memory = self.root / ".runtime/route-autoclose/state.json"
        remembered = json.loads(memory.read_text()) if memory.is_file() else {}
        self.assertNotIn(cycle_id, remembered.get("unsealable", {}))   # a deferred cycle is never written off

    def test_compose_launches_the_work_before_the_route_autoclose_sweep_starts(self):
        import contextlib
        import io
        import work_start
        api = self._api()
        order, seen = [], {}

        def start(route, path, jobs, **_kw):
            order.append("start")
            return {"state": "started", "route_id": route["route_id"]}

        def autoclose(root, trigger, route=None):
            order.append("autoclose")
            seen["stdout"] = out.getvalue()

        argv = ["capability-route.py", "compose", "--slug", "order", "--campaign-key", "k1", "--shape", "direct",
                "--capability", "autopilot-code", "--capability-mode", "dev", "--intensity", "direct",
                "--cwd", str(self.repo), "--artifact-root", str(self.root), "--tracking", "tracked",
                "--prompt-file", str(self.prompt), "--spec-read", "fixture", "--drift-verdict", "within-spec",
                "--artifact-guard", "fixture", "--owner", "codex", "--parent-harness", "codex",
                "--start", "--jobs", str(self.jobs)]
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, {**self.env, "CODEX_THREAD_ID": "order-session"}))
            stack.enter_context(mock.patch.object(sys, "argv", argv))
            stack.enter_context(mock.patch.object(work_start, "start_work", start))
            stack.enter_context(mock.patch.object(api, "_route_autoclose", autoclose))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            self.assertEqual(api.main(), 0)
        self.assertEqual(order, ["start", "autoclose"])
        self.assertIn('"state": "started"', seen["stdout"])   # the receipt was already printed

    def test_r7_interrupted_autoclose_is_finished_by_the_next_sweep(self):
        route_file, route = self.compose("interrupted", "codex", "session-1")
        record = self.write_artifact(route)
        # The sweep died between writing the closure and sealing the cycle.
        script = ("import importlib.util, json, sys; from pathlib import Path; "
                  "spec = importlib.util.spec_from_file_location('cr', sys.argv[1]); "
                  "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); "
                  "p = Path(sys.argv[2]); r = m.verify_route(json.loads(p.read_text()), None, "
                  "allow_stale_registry=True); m.close_route(r, p, None, 'x', allow_unproven=True, "
                  "autoclose={'reason': 'idle', 'trigger': 'compose', 'closed_by': 'runtime', "
                  "'proof': 'not-claimed'})")
        done = subprocess.run([sys.executable, "-c", script, str(CAP), str(route_file)], env=self.env,
                              cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.cycle_record(record["cycle_id"])["state"], "open")
        self.assertIn("cycles_sealed=1", self.sweep())   # at once: the closure is the runtime's own
        sealed = self.cycle_record(record["cycle_id"])
        self.assertEqual((sealed["state"], sealed["cycle_state"]), ("sealed", "abandoned"))
        self.assertNotIn("errors=1", self.sweep())
        self.assertFalse((self.root / ".runtime/inline-finish/v1" / route["route_id"]).exists())
        closed = self.campaign("campaign-close", record["campaign_id"], "--reason", "done")
        self.assertEqual(closed.returncode, 0, closed.stderr + closed.stdout)

    def test_g_a_held_producer_lock_defers_sealing_to_a_later_sweep(self):
        import artifact_admission
        route_file, route = self.compose("locked", "codex", "session-9")
        record = self.write_artifact(route)
        self.age(route_file, "codex", "session-9")
        fd = artifact_admission._acquire_lock(self.root, 1.0)
        try:
            started = time.monotonic()
            self.sweep()
            self.assertLess(time.monotonic() - started, 30)
        finally:
            artifact_admission._release_lock(self.root, fd)
        self.assertEqual(self.outcome(route_file)["autoclose"]["reason"], "idle")
        self.assertEqual(self.cycle_record(record["cycle_id"])["state"], "open")
        self.sweep()
        self.assertEqual(self.cycle_record(record["cycle_id"])["state"], "sealed")

    def test_g_a_running_sweep_makes_the_next_one_skip(self):
        route_file, _route = self.compose("busy-sweep", "codex", "session-9")
        self.age(route_file, "codex", "session-9")
        with (self.root / ".runtime/route-autoclose.lock").open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            self.sweep()
            self.assertIsNone(self.outcome(route_file))
        self.sweep()
        self.assertIsNotNone(self.outcome(route_file))

    # ---- never closes live work ---------------------------------------------
    def test_r1_second_campaign_of_a_live_session_leaves_the_first_alone(self):
        self.claude_alive("S")
        first_file, first = self.compose("first", "claude", "S", campaign="k1")
        record = self.write_artifact(first, "draft.md", "half-written draft\n")
        self.compose("second", "claude", "S", campaign="k2")
        self.assertIsNone(self.outcome(first_file))
        self.assertEqual(self.cycle_record(record["cycle_id"])["state"], "open")

    # ---- compose sweeps only its own campaign ------------------------------------
    def _api(self):
        spec = importlib.util.spec_from_file_location("route_autoclose_capability_api", CAP)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def _other_campaign_ended(self):
        """A stale route of campaign `other-stream` that any root-wide sweep would close and seal."""
        self.claude_crashed("other-session")
        other_file, other = self.compose("other", "claude", "other-session", campaign="other-stream")
        record = self.write_artifact(other)
        self.age(other_file, "claude", "other-session", TWO_HOURS)
        return other_file, other, record

    def _spies(self, stack, RA, artifact_locator, artifact_campaign):
        """Real-calling spies on every read a sweep makes of a cycle record or a campaign folder."""
        targets = [(artifact_producer, "read_cycle_record"), (artifact_producer, "cycle_record_path"),
                   (artifact_producer, "finalize"), (artifact_producer, "_finalize_route"),
                   (artifact_producer, "_read_json"), (artifact_producer, "campaign_dir"),
                   (artifact_producer, "read_campaign"), (artifact_producer, "find_campaign_by_key"),
                   (artifact_producer, "_campaigns_by_key"), (artifact_locator, "read_cycle_binding"),
                   (artifact_campaign, "fold_campaign"), (RA, "_signature"), (RA, "_newest_mtime"),
                   (os, "scandir")]
        return {f"{owner.__name__}.{name}": stack.enter_context(
            mock.patch.object(owner, name, wraps=getattr(owner, name))) for owner, name in targets}

    @staticmethod
    def _touched(spies):
        """Every argument any spied call received, as text, plus the per-spy call lists."""
        return {name: [" ".join(map(str, (*call.args, *call.kwargs.values()))) for call in spy.call_args_list]
                for name, spy in spies.items()}

    def test_compose_never_scans_or_seals_another_campaigns_cycles(self):
        import contextlib
        import artifact_campaign
        import artifact_locator
        import route_autoclose as RA
        other_file, other, other_record = self._other_campaign_ended()
        other_dir = artifact_producer.campaign_dir(self.root, other_record["campaign_id"])
        other_cycle = other_record["cycle_id"]
        other_record_path = artifact_producer.cycle_record_path(self.root, other_cycle)
        mine_file, mine = self.compose("mine", "codex", "mine-session", campaign="scope-mine")
        mine_record = self.cycle(mine)
        # The public path: a compose in the other campaign's neighbour leaves it alone.
        self.compose("observer-compose", "codex", "observer", campaign="scope-mine", start=False)
        self.assertIsNone(self.outcome(other_file))
        self.assertEqual(self.cycle_record(other_cycle)["state"], "open")
        # The same sweep in process, watching what it reads.
        found = RA.campaign_of_route(self.root, mine)
        self.assertEqual(found, (mine_record["campaign_id"], self.cycle_dir(mine_record).parent))
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, self.env))
            spies = self._spies(stack, RA, artifact_locator, artifact_campaign)
            summary = RA.sweep(self.root, api=self._api(), trigger="compose",
                               scope_campaign_id=found[0], scope_dir=found[1])
        self.assertEqual(summary["errors"], [])
        touched = self._touched(spies)
        everything = [item for calls in touched.values() for item in calls]
        self.assertNotIn(other_cycle, " ".join(everything))                       # (1) no cycle id
        self.assertNotIn(str(other_dir), " ".join(everything))                    # (2) no campaign folder
        self.assertNotIn(str(other_record_path), " ".join(everything))            #     no record file
        cycles_dir = str(self.root / ".runtime/artifact-producer/v1/cycles")
        self.assertFalse([item for item in touched["os.scandir"] if item.strip() == cycles_dir])   # (3)
        self.assertTrue(any(mine_record["cycle_id"] in item                                       # (4) the spies were live
                            for item in touched["artifact_producer.read_cycle_record"]))
        self.assertTrue(touched["artifact_locator.read_cycle_binding"])
        self.assertEqual(touched["artifact_producer.find_campaign_by_key"], [])
        self.assertEqual(touched["artifact_producer._campaigns_by_key"], [])
        self.assertEqual(self.cycle_record(other_cycle)["state"], "open")

    def test_compose_whose_campaign_is_not_found_by_name_skips_its_sweep_and_reads_no_campaign(self):
        import contextlib
        import artifact_campaign
        import artifact_locator
        import route_autoclose as RA
        other_file, other, other_record = self._other_campaign_ended()
        self.compose("mine", "codex", "mine-session", campaign="scope-mine")
        spec = {"campaign_key": "brand-new-stream", "route_id": "rt-" + "0" * 16}
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, self.env))
            spies = self._spies(stack, RA, artifact_locator, artifact_campaign)
            sweeps = stack.enter_context(mock.patch.object(RA, "sweep", wraps=RA.sweep))
            self.assertIsNone(RA.campaign_of_route(self.root, spec))
            self._api()._route_autoclose(self.root, "compose", spec)
        self.assertEqual(sweeps.call_count, 0)                  # the miss skips the sweep
        touched = self._touched(spies)
        for name in ("artifact_producer._read_json", "artifact_campaign.fold_campaign",
                     "artifact_producer.find_campaign_by_key", "artifact_producer._campaigns_by_key",
                     "artifact_producer.read_cycle_record", "artifact_locator.read_cycle_binding"):
            self.assertEqual(touched[name], [], name)           # no campaign.json and no cycle opened
        self.assertIsNone(self.outcome(other_file))
        # A key that only prefixes another stream's folder name finds nothing.
        self.assertIsNone(RA.campaign_of_route(self.root, {"campaign_key": "other"}))

    def test_campaign_status_observes_and_writer_closes_its_selected_campaign(self):
        # Query leaves ended routes alone; the existing writer owns the sweep.
        other_file, other, other_record = self._other_campaign_ended()
        mine_file, mine = self.compose("mine", "codex", "mine-session", campaign="scope-mine")
        self.assertIsNone(self.outcome(other_file))
        before = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        observed = self.campaign("campaign-status", self.cycle(mine)["campaign_id"])
        self.assertEqual(observed.returncode, 0, observed.stderr)
        self.assertEqual(before, {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()})
        self.assertIsNone(self.outcome(other_file))
        done = self.campaign("campaign-close", other_record["campaign_id"], "--reason", "done")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.outcome(other_file)["autoclose"]["reason"], "campaign-close")
        self.assertEqual(self.cycle_record(other_record["cycle_id"])["state"], "sealed")

    def test_campaign_close_preserves_unrelated_stale_routes_and_empty_controls(self):
        other_file, _other, other_record = self._other_campaign_ended()
        self.claude_crashed("empty-session")
        empty_file, empty = self.compose("empty-other", "claude", "empty-session",
                                         campaign="empty-other-stream")
        empty_record = self.cycle(empty)
        self.age(empty_file, "claude", "empty-session", TWO_HOURS)
        self.claude_crashed("mine-ended")
        mine_file, mine = self.compose("close-mine", "claude", "mine-ended",
                                      campaign="close-selected-stream")
        mine_record = self.write_artifact(mine)
        self.age(mine_file, "claude", "mine-ended", TWO_HOURS)
        protected = [other_file, empty_file]
        for record in (other_record, empty_record):
            protected.extend(p for p in self.cycle_dir(record).rglob("*") if p.is_file())
            protected.append(artifact_producer.cycle_record_path(self.root, record["cycle_id"]))
        before = {str(path): path.read_bytes() for path in protected}
        done = self.campaign("campaign-close", mine_record["campaign_id"], "--reason", "done")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["status"], "satisfied")
        self.assertEqual(before, {str(path): path.read_bytes() for path in protected})
        self.assertIsNone(self.outcome(other_file))
        self.assertIsNone(self.outcome(empty_file))
        self.assertEqual(self.cycle_record(empty_record["cycle_id"])["state"], "open")
        self.assertEqual(self.outcome(mine_file)["autoclose"]["reason"], "campaign-close")

    def test_r6_sub_agent_sharing_the_session_id_leaves_the_parent_route_alone(self):
        self.claude_alive("parent")
        parent_file, _parent = self.compose("parent-work", "claude", "parent")
        self.compose("subagent-work", "claude", "parent")
        self.assertIsNone(self.outcome(parent_file))

    def test_r2_route_waiting_on_a_human_gate_is_never_closed(self):
        self.claude_crashed("claude-crashed")
        route_file, route = self.compose("gated", "claude", "claude-crashed", intensity="quick", start=False)
        self.raise_gate(route)
        self.age(route_file, "claude", "claude-crashed")
        self.sweep()
        self.assertIsNone(self.outcome(route_file))

    def test_r3_ended_owner_at_a_gate_is_not_closed_by_the_next_compose(self):
        route_file, route = self.compose("gated2", "codex", "session-1", intensity="quick", start=False)
        self.raise_gate(route)
        metadata = f"attempt_id=att-owner-ended,worker_type=owner,owner_route_id={route['route_id']}"
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - TWO_HOURS))
        self.jobs.write_text(f"{old}\tdone\t{self.repo}\t{self.repo}\towner\t{metadata}\n")
        self.age(route_file, "codex", "session-1")
        self.compose("side-question", "codex", "session-1")
        self.sweep()
        self.assertIsNone(self.outcome(route_file))

    def test_r4_claude_session_this_host_cannot_see_waits_for_the_day_rule(self):
        self.claude_alive("S-live")
        route_file, _route = self.compose("busy", "claude", "S-live")
        self.age(route_file, "claude", "S-live", TWO_HOURS)
        other = self.base / "other-profile"
        (other / "sessions").mkdir(parents=True)
        self.env["CLAUDE_CONFIG_DIR"] = str(other)   # another host / profile
        self.sweep()
        self.assertIsNone(self.outcome(route_file))

    def test_attempt_the_registry_still_holds_is_never_closed(self):
        route_file, route = self.compose("owned", "codex", "session-9", intensity="quick", start=False)
        metadata = f"attempt_id=att-live-owner,worker_type=owner,owner_route_id={route['route_id']}"
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.jobs.write_text(f"{stamp}\trunning\t{self.repo}\t{self.repo}\towned\t{metadata}\n")
        self.age(route_file, "codex", "session-9")
        self.sweep()
        self.assertIsNone(self.outcome(route_file))
        self.jobs.write_text(f"2026-01-01T00:00:00Z\tdone\t{self.repo}\t{self.repo}\towned\t{metadata}\n")
        self.sweep()
        self.assertIsNone(self.outcome(route_file))   # judged again only after RECHECK_SECONDS
        self.later()
        self.sweep()
        self.assertEqual(self.outcome(route_file)["autoclose"]["reason"], "idle")

    def test_n6_pass_owner_with_pending_settlement_is_left_to_the_runtime(self):
        route_file, route = self.compose("owned", "codex", "session-9", intensity="quick", start=False)
        slot = self.root / ".runtime/terminal-commits/v1" / route["route_id"] / "att-owner"
        slot.mkdir(parents=True)
        (slot / "producer-binding.json").write_text("{}")   # written at owner launch
        metadata = (f"attempt_id=att-owner,worker_type=owner,dispatch_depth=1,owner_route_id={route['route_id']},"
                    f"owner_route_file={route_file},owner_route_hash={route['route_hash']},"
                    "workflow_completion=runtime-v1,failure_class=pass")
        self.jobs.write_text(f"2026-01-01T00:00:00Z\tdone\t{self.repo}\t{self.repo}\towned\t{metadata}\n")
        self.age(route_file, "codex", "session-9")
        # dispatch_terminal_commit.owner_completion_pending: pending or unknown both keep the route.
        self.sweep()
        self.assertIsNone(self.outcome(route_file))

    def test_live_resource_run_bound_to_the_route_is_never_closed(self):
        route_file, route = self.compose("training", "codex", "session-9")
        runner = self.spawn("train.py")
        identity = {"pid": runner.pid, "starttime": _proc_start(runner.pid),
                    "command_hash": hashlib.sha256(Path(f"/proc/{runner.pid}/cmdline").read_bytes()).hexdigest()}
        registry = self.base / "resource-runs.json"
        registry.write_text(json.dumps({"schema_version": 1, "runs": {
            "train": {**identity, "route_file": str(route_file), "route_id": route["route_id"],
                      "node": "inline", "status": "running"}}}))
        self.resource_index.write_text(json.dumps({"schema_version": 1, "registries": {
            "r": {"path": str(registry)}}}))
        self.age(route_file, "codex", "session-9")
        self.sweep()
        self.assertIsNone(self.outcome(route_file))
        runner.kill(); runner.wait()
        self.sweep()   # gone here, but a run on another host is gone here too: no end record, still live
        self.assertIsNone(self.outcome(route_file))
        data = json.loads(registry.read_text())
        data["runs"]["train"].update(status="failed", exit_code=-9)   # the runner records the end
        registry.write_text(json.dumps(data))
        self.later()
        self.sweep()
        self.assertEqual(self.outcome(route_file)["autoclose"]["reason"], "idle")

    def remote_run(self, route_file, **extra):
        """A run registered from another host: its pid means nothing here."""
        log = self.repo / "runs" / "train.log"
        log.parent.mkdir(exist_ok=True)
        log.write_text("epoch 1\n")
        registry = self.base / "runs.json"
        registry.write_text(json.dumps({"schema_version": 1, "runs": {"r1": {
            "run_id": "r1", "pid": 4000000, "starttime": "123", "command_hash": "ab" * 32,
            "cwd": str(self.repo), "log": str(log), "sentinel": str(log) + ".exit",
            "route": str(route_file), "node": "full-run", "status": "running", **extra}}}))
        self.resource_index.write_text(json.dumps({"schema_version": 1, "registries": {
            "k": {"path": str(registry)}}}))
        return log

    def test_n2_run_on_another_host_counts_as_live_until_it_records_an_end(self):
        route_file, route = self.compose("remote-training", "codex", "session-b")
        log = self.remote_run(route_file)
        self.age(route_file, "codex", "session-b")
        old = time.time() - EIGHT_DAYS
        os.utime(log, (old, old))
        self.sweep()
        self.assertIsNone(self.outcome(route_file))
        Path(str(log) + ".exit").write_text("0")   # the wrapper's exit sentinel
        self.later()
        self.sweep()
        self.assertEqual(self.outcome(route_file)["autoclose"]["reason"], "idle")

    def test_n2_a_bound_runs_fresh_log_counts_as_activity(self):
        route_file, route = self.compose("remote-log", "codex", "session-b")
        self.remote_run(route_file, status="succeeded")   # ended, but it just wrote
        self.age(route_file, "codex", "session-b")
        self.sweep()
        self.assertIsNone(self.outcome(route_file))

    def test_n7_unreadable_resource_index_closes_nothing(self):
        route_file, route = self.compose("training-local", "codex", "session-b")
        self.resource_index.write_text('{"schema_version": 1, "registries": ')
        self.age(route_file, "codex", "session-b")
        self.assertNotIn("route_autoclose closed", self.sweep())
        self.assertIsNone(self.outcome(route_file))

    def test_n1_week_rule_covers_a_gate_this_host_cannot_see(self):
        route_file, route = self.compose("gated-elsewhere", "codex", "session-b", intensity="quick", start=False)
        self.age(route_file, "codex", "session-b", TWO_DAYS)
        self.sweep()
        self.assertIsNone(self.outcome(route_file))

    def test_cycle_a_live_process_writes_into_is_never_closed(self):
        route_file, route = self.compose("logging", "codex", "session-9")
        record = self.write_artifact(route, "train.log", "epoch 1\n")
        log = self.cycle_dir(record) / "artifacts" / "documents" / "train.log"
        writer = subprocess.Popen([sys.executable, "-c", "import sys, time; f = open(sys.argv[1], 'a'); "
                                   "time.sleep(300)", str(log)])
        self.processes.append(writer)
        time.sleep(0.5)
        self.age(route_file, "codex", "session-9")
        self.sweep()
        self.assertIsNone(self.outcome(route_file))
        writer.kill(); writer.wait()
        self.later()
        self.sweep()
        self.assertIsNotNone(self.outcome(route_file))

    def test_live_and_resumed_claude_sessions_are_never_closed(self):
        self.claude_alive("claude-live")
        live_file, _live = self.compose("busy", "claude", "claude-live")
        resumed_file, _resumed = self.compose("resumed", "claude", "claude-old")
        proc = self.spawn("--resume", "claude-old")
        self.claude_record(proc, "claude-new")
        self.age(live_file, "claude", "claude-live")
        self.age(resumed_file, "claude", "claude-old")
        self.sweep()
        self.assertIsNone(self.outcome(live_file))
        self.assertIsNone(self.outcome(resumed_file))

    def autoclosed(self):
        route_file, route = self.compose("overnight", "codex", "session-b")
        record = self.write_artifact(route)
        self.age(route_file, "codex", "session-b")
        self.sweep()
        self.assertEqual(self.outcome(route_file)["autoclose"]["reason"], "idle")
        return route_file, route, record

    def test_n3_finish_on_an_automatically_closed_route_succeeds(self):
        route_file, route, record = self.autoclosed()
        summary = self.base / "summary.md"
        summary.write_text("done\n")
        done = self.run_as("codex", "session-b", "finish", "--route", route_file, "--evidence",
                           self.cycle_dir(record) / "artifacts/documents/report.md", "--summary-file", summary)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["state"], "already-closed-automatically")
        self.assertIn("already closed automatically", done.stderr)

    def test_finish_without_a_route_takes_this_sessions_latest_route_here(self):
        route_file, route, record = self.autoclosed()
        summary = self.base / "summary.md"
        summary.write_text("done\n")
        self.env["AGENT_ARTIFACT_ROOT"] = str(self.root)
        evidence = self.cycle_dir(record) / "artifacts/documents/report.md"
        done = self.run_as("codex", "session-b", "finish", "--evidence", evidence, "--summary-file", summary)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["state"], "already-closed-automatically")
        by_id = self.run_as("codex", "session-b", "finish", "--route", route["route_id"],
                            "--evidence", evidence, "--summary-file", summary)
        self.assertEqual(by_id.returncode, 0, by_id.stderr)
        other = self.run_as("codex", "session-c", "finish", "--evidence", evidence, "--summary-file", summary)
        self.assertNotEqual(other.returncode, 0)
        self.assertIn("route-required", other.stderr)

    def test_n3_start_on_an_automatically_closed_route_hands_back_a_compose(self):
        route_file, route, _record = self.autoclosed()
        done = self.run_as("codex", "session-b", "start", "--route", route_file, "--jobs", self.jobs)
        self.assertEqual(done.returncode, 0, done.stderr)
        receipt = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual((receipt["state"], receipt["parent_next"]), ("autoclosed", "compose"))
        again = subprocess.run(receipt["parent_next_command"], shell=True, cwd=self.repo, capture_output=True,
                               text=True, env={**self.env, "CODEX_THREAD_ID": "session-b"})
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertNotEqual(json.loads(again.stdout.strip().splitlines()[-1])["route_id"], route["route_id"])

    def test_review3_worker_environment_cannot_move_a_test_into_a_real_root(self):
        decoy = self.base / "decoy-artifact-root"
        decoy.mkdir()
        worker = {"AGENT_ARTIFACT_ROOT": str(decoy), "AGENT_ARTIFACT_OUTPUT_DIR": str(decoy / "out"),
                  "AGENT_ARTIFACT_CYCLE_ID": "cyc_" + "d" * 32, "AGENT_ARTIFACT_CAMPAIGN_KEY": "k1",
                  "AGENT_WORKFLOW_ROOT": str(decoy / "workflow")}
        with mock.patch.dict(os.environ, worker):
            self.assertFalse(set(worker) & set(isolated_env()))
            route_file, route, _record = self.autoclosed()
            done = self.run_as("codex", "session-b", "start", "--route", route_file, "--jobs", self.jobs)
        command = json.loads(done.stdout.strip().splitlines()[-1])["parent_next_command"]
        # The leak's mechanism: the compose command a returning session runs, under a worker env.
        again = subprocess.run(command, shell=True, cwd=self.repo, capture_output=True, text=True,
                               env={**self.env, **worker, "CODEX_THREAD_ID": "session-b"})
        self.assertEqual(again.returncode, 0, again.stderr)
        receipt = json.loads(again.stdout.strip().splitlines()[-1])
        self.assertTrue(receipt["route_file"].startswith(str(self.root)), receipt["route_file"])
        self.assertEqual(sorted(decoy.rglob("*")), [])

    def test_review3_write_into_an_automatically_closed_cycle_is_allowed(self):
        route_file, route, record = self.autoclosed()
        target = self.cycle_dir(record) / "artifacts" / "documents" / "late.md"
        done = self.run_as("codex", "session-b", "check-write", "--artifact-root", self.root,
                           "--file", target, program=PRODUCER)
        verdict = json.loads(done.stdout)
        # §45 D-123: the automatic close is a record of the cycle, not a lock on its folder.
        self.assertEqual(verdict["verdict"], "allow")
        self.assertEqual(verdict["cycle_id"], record["cycle_id"])

    def test_review3_a_cycle_that_cannot_seal_is_not_retried_until_its_evidence_changes(self):
        route_file, route = self.compose("stuck", "codex", "session-1")
        record = self.write_artifact(route)
        self.run_as("codex", "session-1", "close", "--route", route_file, "--allow-unproven")
        old = time.time() - TWO_HOURS
        os.utime(route_file.with_name(route_file.stem + ".outcome.json"), (old, old))
        cycle_file = self.root / ".runtime/artifact-producer/v1/cycles" / (record["cycle_id"] + ".json")
        stuck = json.loads(cycle_file.read_text())
        stuck["parent_cycle_id"] = "cyc_" + "e" * 32   # a parent that never seals
        cycle_file.write_text(json.dumps(stuck))
        self.assertIn("cycles_left_open=1", self.sweep())
        memory = json.loads((self.root / ".runtime/route-autoclose/state.json").read_text())
        self.assertIn(record["cycle_id"], memory["unsealable"])
        self.assertNotIn("cycles_left_open", self.sweep())   # remembered: no second attempt
        os.utime(cycle_file, None)                          # new evidence
        self.assertIn("cycles_left_open=1", self.sweep())

    def test_review3_a_kept_route_is_judged_again_only_after_the_recheck_interval(self):
        import route_autoclose
        route_file, route = self.compose("owned-later", "codex", "session-9", intensity="quick", start=False)
        metadata = f"attempt_id=att-live-owner,worker_type=owner,owner_route_id={route['route_id']}"
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.jobs.write_text(f"{stamp}\trunning\t{self.repo}\t{self.repo}\towned\t{metadata}\n")
        self.age(route_file, "codex", "session-9")
        self.sweep()
        state = json.loads((self.root / ".runtime/route-autoclose/state.json").read_text())
        row = state["kept"]["route:" + route["route_id"]]
        self.assertEqual(row["reason"], "owner-live")
        self.assertAlmostEqual(row["until"] - time.time(), route_autoclose.RECHECK_SECONDS, delta=120)

    def test_normal_finish_consumes_prior_unproven_close(self):
        route_file, route = self.compose("hand-closed", "codex", "session-1")
        record = self.write_artifact(route)
        self.run_as("codex", "session-1", "close", "--route", route_file, "--allow-unproven")
        summary = self.base / "summary.md"
        summary.write_text("done\n")
        done = self.run_as("codex", "session-1", "finish", "--route", route_file, "--evidence",
                           self.cycle_dir(record) / "artifacts/documents/report.md", "--summary-file", summary)
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        outcome = self.outcome(route_file)
        self.assertTrue(outcome["terminal_gate_proven"])
        manifest = json.loads((self.cycle_dir(record) / "manifest.json").read_text())
        self.assertEqual(manifest["cycle"]["state"], "completed")

    def test_the_sweeping_session_keeps_its_own_routes(self):
        route_file, _route = self.compose("mine", "codex", "session-1")
        self.age(route_file, "codex", "session-1")
        self.compose("next", "codex", "session-1")
        self.assertIsNone(self.outcome(route_file))

    def test_e_an_oversized_cycle_counts_as_active(self):
        route_file, route = self.compose("big", "codex", "session-9")
        record = self.write_artifact(route)
        bulk = self.cycle_dir(record) / "artifacts" / "bulk"
        bulk.mkdir()
        for index in range(2100):
            (bulk / f"{index}.txt").write_text("x")
        self.age(route_file, "codex", "session-9")
        self.sweep()
        self.assertIsNone(self.outcome(route_file))

    def test_f_status_stays_read_only(self):
        route_file, route = self.compose("listed", "codex", "session-9")
        self.age(route_file, "codex", "session-9")
        self.assertIn(route["route_id"], self.status())
        self.assertIsNone(self.outcome(route_file))

    # ---- campaign close -------------------------------------------------------
    def test_e_campaign_close_by_its_worker_is_not_refused_by_its_open_direct(self):
        route_file, route = self.compose("member", "codex", "session-1", campaign="k2")
        campaign = self.write_artifact(route)["campaign_id"]
        status = json.loads(self.campaign("campaign-status", campaign).stdout)
        # §45 D-127: what refuses a close is a route that is still open, not an unclosed cycle.
        self.assertEqual(status["close_refusal"]["reason"], "campaign-cycle-provisional-active")
        closed = self.run_as("codex", "session-1", "campaign-close", "--artifact-root", self.root,
                             "--campaign", campaign, "--reason", "report shipped", program=PRODUCER)
        self.assertEqual(closed.returncode, 0, closed.stderr + closed.stdout)
        self.assertEqual(self.outcome(route_file)["autoclose"]["reason"], "campaign-close")
        self.assertEqual(json.loads(self.campaign("campaign-status", campaign).stdout)["state"], "satisfied")

    def test_e_campaign_close_takes_an_hour_quiet_member_but_not_fresh_work(self):
        route_file, route = self.compose("member", "opencode", "ses-oc", campaign="k3")
        campaign = self.write_artifact(route)["campaign_id"]
        fresh = self.campaign("campaign-close", campaign, "--reason", "done")
        self.assertNotEqual(fresh.returncode, 0)
        self.assertIsNone(self.outcome(route_file))
        self.age(route_file, "opencode", "ses-oc", TWO_HOURS)
        closed = self.campaign("campaign-close", campaign, "--reason", "done")
        self.assertEqual(closed.returncode, 0, closed.stderr + closed.stdout)
        self.assertEqual(self.outcome(route_file)["autoclose"]["reason"], "campaign-close")

    def test_review4_campaign_close_judges_its_members_despite_the_recheck_interval(self):
        route_file, route = self.compose("member-owned", "codex", "session-9", campaign="k4")
        campaign = self.write_artifact(route)["campaign_id"]
        metadata = f"attempt_id=att-owner,worker_type=owner,owner_route_id={route['route_id']}"
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.jobs.write_text(f"{stamp}\trunning\t{self.repo}\t{self.repo}\towned\t{metadata}\n")
        self.age(route_file, "codex", "session-9", TWO_HOURS)
        refused = self.campaign("campaign-close", campaign, "--reason", "done")
        self.assertNotEqual(refused.returncode, 0)   # the owner still runs
        self.assertIsNone(self.outcome(route_file))
        # The owner just ended; the user closes the campaign within RECHECK_SECONDS.
        self.jobs.write_text(f"2026-01-01T00:00:00Z\tdone\t{self.repo}\t{self.repo}\towned\t{metadata}\n")
        closed = self.campaign("campaign-close", campaign, "--reason", "done")
        self.assertEqual(closed.returncode, 0, closed.stderr + closed.stdout)
        self.assertEqual(self.outcome(route_file)["autoclose"]["reason"], "campaign-close")

    # ---- the checkout and everything outside the artifact root ----------------
    def test_r5_checkout_bytes_and_index_are_untouched_and_git_is_only_read(self):
        shim_dir = self.base / "shim"
        shim_dir.mkdir()
        log = self.base / "git.log"
        real_git = subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()
        (shim_dir / "git").write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> " + str(log)
                                      + "\nexec " + real_git + " \"$@\"\n")
        (shim_dir / "git").chmod(0o755)
        route_file, route = self.compose("edited", "codex", "session-9")
        campaign = self.write_artifact(route)["campaign_id"]
        (self.repo / "README.md").write_text("user edit in progress\n", encoding="utf-8")
        os.utime(self.repo / "README.md", (time.time() + 5, time.time() + 5))   # stat-stale index
        self.age(route_file, "codex", "session-9")
        index = self.repo / ".git" / "index"
        before = {path: path.read_bytes() for path in self.repo.rglob("*") if path.is_file()}
        index_before = (index.stat().st_mtime_ns, index.read_bytes())
        self.env["PATH"] = str(shim_dir) + os.pathsep + self.env["PATH"]
        closed = self.campaign("campaign-close", campaign, "--reason", "done")
        self.assertIn("route_autoclose closed=1", closed.stderr)
        self.assertEqual({path: path.read_bytes() for path in self.repo.rglob("*") if path.is_file()}, before)
        self.assertEqual((index.stat().st_mtime_ns, index.read_bytes()), index_before)
        calls = log.read_text().splitlines() if log.is_file() else []
        self.assertTrue(all("rev-parse" in call.split() for call in calls), calls)

    def test_r8_a_sweep_writes_nothing_outside_the_artifact_root(self):
        route_file, route = self.compose("outside", "codex", "session-9")
        campaign = self.write_artifact(route)["campaign_id"]
        self.age(route_file, "codex", "session-9")

        def snapshot():
            return {str(path): path.stat().st_mtime_ns for path in self.base.rglob("*")
                    if path.is_file() and not path.is_relative_to(self.root) and not path.is_relative_to(self.repo)}

        before = snapshot()
        done = self.campaign("campaign-close", campaign, "--reason", "done")
        self.assertIn("route_autoclose closed=1", done.stderr)
        after = snapshot()
        self.assertEqual(sorted(key for key in after if before.get(key) != after[key]), [])


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "utilities" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PRODUCER_FIXTURE = _load("route_autoclose_producer_fixture", "artifact_producer.test.py")


class LeftoverCyclesTest(PRODUCER_FIXTURE.ProducerTestBase):
    """Sweep-level cases for what earlier sweeps left open: cycles of a closed
    lineage other cycles began in, and workflow-group members that ended empty.
    Lineages are built with the producer fixture and swept in-process; every
    path is a temporary root."""

    fixture = PRODUCER_FIXTURE

    def setUp(self):
        super().setUp()
        self.P, self.R = self.fixture.P, self.fixture.R
        lineage = self.fixture.RouteLineageBindingTest
        for name in ("_root_route", "_publish_root", "_continuation", "_begin", "_close"):
            setattr(self, name, getattr(lineage, name).__get__(self))
        self._campaign_writes = 0

    # -- fixtures -------------------------------------------------------
    def _begin_all(self, routes):
        """One cycle per route, the way a resumed route began a fresh cycle inside an
        earlier cycle's lineage: the earlier ones are held not-open while it begins."""
        cycles = []
        for route in routes:
            held = [self.P.read_cycle_record(self.root, cycle["cycle_id"]) for cycle in cycles]
            for record in held:
                self.P._write_cycle_record(self.root, dict(record, state="abandoned"), exclusive=False)
            cycles.append(self._begin(route))
            for record in held:
                self.P._write_cycle_record(self.root, record, exclusive=False)
        for index, cycle in enumerate(cycles):
            self.write_output(cycle, f"plans/leftover-{index}/note.md", f"cycle {index}\n".encode())
        return cycles

    def _close_unproven(self, route):
        path = self.R.canonical_route_path(self.root, route["route_id"])
        raw = json.loads(path.read_text(encoding="utf-8"))
        checked = self.R.verify_route(dict(raw), None, allow_stale_registry=True)
        self.R.close_route(checked, path, None, "fixture", allow_unproven=True,
                           autoclose={"reason": "idle", "trigger": "compose", "closed_by": "runtime",
                                      "proof": "not-claimed", "at": "2026-09-30T00:00:00Z"})

    def _sweep(self, hours=2):
        import route_autoclose
        return route_autoclose.sweep(self.root, api=self.R, trigger="compose", now=time.time() + hours * 3600)

    def _record(self, cycle):
        return self.P.read_cycle_record(self.root, cycle["cycle_id"])

    def _manifest_routes(self, cycle):
        record = self._record(cycle)
        directory = self.P.cycle_dir(self.root, record["campaign_id"], cycle["cycle_id"], record)
        document = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        return [row["route_id"] for row in document["routes"]]

    def _results(self, summary):
        return {row["cycle_id"]: row["cycle"] for row in summary["cycles"]}

    # -- lineages ---------------------------------------------------------
    def test_handed_over_lineage_cycles_seal_on_their_own_routes(self):
        a = self._root_route("leftover-own")
        self._publish_root(a)
        b = self._continuation(a)
        c = self._continuation(b)
        cycles = self._begin_all([a, b, c])
        for route in (a, b, c):
            self._close_unproven(route)
        started = time.monotonic()
        summary = self._sweep()
        elapsed = time.monotonic() - started
        print(f"route_autoclose_test lineage-sweep seconds={elapsed:.3f}", file=sys.stderr)
        self.assertEqual(summary["errors"], [])
        self.assertEqual(self._results(summary), {cycle["cycle_id"]: "cycle-abandoned" for cycle in cycles})
        for cycle, route in zip(cycles, (a, b, c)):
            self.assertEqual(self._record(cycle)["state"], "sealed")
            self.assertEqual(self._manifest_routes(cycle), [route["route_id"]])

    def test_open_lineage_tree_keeps_ambiguity(self):
        import route_autoclose
        a = self._root_route("leftover-live")
        self._publish_root(a)
        b = self._continuation(a)
        first, second = self._begin_all([a, b])
        self._close_unproven(a)   # B stays open: the lineage is live
        summary = self._sweep()
        self.assertTrue(all(row["cycle"].startswith("cycle-left-open") for row in summary["cycles"]), summary)
        self.assertEqual(self._record(first)["state"], "open")
        self.assertEqual(self._record(second)["state"], "open")
        self.assertFalse((self.P.cycle_dir(self.root, self._record(first)["campaign_id"], first["cycle_id"])
                          / "manifest.json").exists())
        # The ambiguity is not bypassed for a live tree, whatever record is offered.
        self.assertTrue(route_autoclose._seal_cycle(self.root, b, False, record=self._record(first))
                        .startswith("cycle-unresolved:route-cycle-binding-ambiguous"))
        self.assertEqual(self._record(first)["state"], "open")

    def test_lineage_fork_cycle_seals(self):
        a = self._root_route("leftover-fork")
        self._publish_root(a)
        x = self._continuation(a, reason="leftover-fork-x")
        y = self._continuation(a, reason="leftover-fork-y")
        first = self._begin(a)
        record = self._record(first)
        self.P._write_cycle_record(self.root, dict(record, state="abandoned"), exclusive=False)
        second = self._begin(y)
        self.P._write_cycle_record(self.root, record, exclusive=False)
        for cycle in (first, second):
            self.write_output(cycle, "plans/leftover/note.md", b"leftover\n")
        # This case represents used fork branches, not merely published candidates.
        self.jobs.write_text("".join(
            f"2026-10-09\tdone\t{self.R.ROOT}\t{self.R.ROOT}\tchild\t"
            f"route_id={route['route_id']},attempt_id=att-{route['route_id']},launch_started=1\n"
            for route in (x, y)))
        for route in (a, x, y):
            self._close_unproven(route)
        summary = self._sweep()
        self.assertEqual(self._results(summary), {first["cycle_id"]: "cycle-abandoned",
                                                  second["cycle_id"]: "cycle-abandoned"})
        self.assertEqual(self._manifest_routes(first), [x["route_id"]])
        self.assertEqual(self._manifest_routes(second), [y["route_id"]])

    def test_closed_unused_fork_keeps_predecessor_as_manifest_route(self):
        a = self._root_route("leftover-unused-fork")
        self._publish_root(a)
        x = self._continuation(a, reason="unused-x")
        y = self._continuation(a, reason="unused-y")
        cycle = self._begin(a)
        self.write_output(cycle, "plans/leftover/note.md", b"predecessor work\n")
        for route in (a, x, y):
            self._close_unproven(route)
        self.assertEqual(self._results(self._sweep()), {cycle["cycle_id"]: "cycle-abandoned"})
        self.assertEqual(self._manifest_routes(cycle), [a["route_id"]])

    def test_a_lineage_walk_past_its_deadline_leaves_the_cycle_open_and_completes_without_one(self):
        import route_autoclose
        a = self._root_route("leftover-deadline")
        self._publish_root(a)
        b = self._continuation(a)
        c = self._continuation(b)
        cycle = self._begin(a)
        self.write_output(cycle, "plans/leftover/note.md", b"leftover\n")
        self.jobs.write_text(f"2026-10-09\tdone\t{self.R.ROOT}\t{self.R.ROOT}\tchild\t"
                             f"route_id={c['route_id']},attempt_id=att-deadline-child,launch_started=1\n")
        for route in (a, b, c):
            self._close_unproven(route)
        record = self._record(cycle)
        expired = time.monotonic() - 1.0
        self.assertEqual(route_autoclose._seal_cycle(self.root, c, False, record=record, deadline=expired),
                         "cycle-left-open:scan-in-progress")
        self.assertEqual(self._record(cycle)["state"], "open")
        self.assertFalse((self.P.cycle_dir(self.root, record["campaign_id"], cycle["cycle_id"], record)
                          / "manifest.json").exists())
        self.assertEqual(route_autoclose._seal_cycle(self.root, c, False, record=record), "cycle-abandoned")
        self.assertEqual(self._record(cycle)["state"], "sealed")

    def test_the_routes_directory_is_read_once_while_it_is_unchanged(self):
        a = self._root_route("leftover-edges")
        self._publish_root(a)
        b = self._continuation(a)
        c = self._continuation(b)
        cycle = self._begin(a)
        directory = self.P._routes_dir(self.root)
        stale = time.time() - 3600
        os.utime(directory, (stale, stale))
        reads, real_qualify = [], self.P._qualified_continuation

        def spy(stem, candidate, *args, **kw):
            reads.append(stem)   # once per route file the scan parsed
            return real_qualify(stem, candidate, *args, **kw)

        def walk():
            record = self._record(cycle)
            return (self.P._lineage_children(self.root, a["route_id"], a["route_hash"]),
                    self.P._finalize_route(self.root, record)["route_id"],
                    self.P.closed_lineage_handover(self.root, record))

        with mock.patch.object(self.P, "_qualified_continuation", spy):
            first = walk()
            read_once = len(reads)
            self.assertGreaterEqual(read_once, 3)            # the scan really parsed the routes
            for _ in range(3):
                self.assertEqual(walk(), first)
            self.assertEqual(len(reads), read_once)          # an unchanged directory is not read again
            self.assertEqual(first[1], c["route_id"])
            d = self._continuation(c)                        # the listing changes: it is read again
            os.utime(directory, (stale - 100, stale - 100))
            before = len(reads)
            self.assertEqual([row["route_id"] for row in
                              self.P._lineage_children(self.root, c["route_id"], c["route_hash"])], [d["route_id"]])
            self.assertEqual(len(reads) - before, 1)         # only the new route file was parsed
            self.assertEqual(self.P._finalize_route(self.root, self._record(cycle))["route_id"], d["route_id"])

    def test_an_edge_file_rewritten_in_place_is_read_again(self):
        a = self._root_route("leftover-rewrite")
        self._publish_root(a)
        b = self._continuation(a)
        c = self._continuation(b)
        cycle = self._begin(a)
        directory = self.P._routes_dir(self.root)
        stale = time.time() - 3600
        os.utime(directory, (stale, stale))
        self.assertEqual(self.P._finalize_route(self.root, self._record(cycle))["route_id"], c["route_id"])
        # Rewritten in place (same inode, the directory's signature unchanged): its sealed hash no
        # longer recomputes, so it is no longer a continuation of b.
        path = directory / f"{c['route_id']}.json"
        tampered = json.loads(path.read_text())
        tampered["slug"] = "rewritten-in-place"
        path.write_text(json.dumps(tampered))
        os.utime(path, ns=(time.time_ns(), time.time_ns() + 1_000_000_000))
        os.utime(directory, (stale, stale))
        self.assertEqual(self.P._lineage_children(self.root, b["route_id"], b["route_hash"]), [])
        self.assertEqual(self.P._finalize_route(self.root, self._record(cycle))["route_id"], b["route_id"])

    def test_finalize_keeps_the_sweep_deadline_for_its_own_lineage_walk(self):
        a = self._root_route("leftover-inner-walk")
        self._publish_root(a)
        b = self._continuation(a)
        c = self._continuation(b)
        cycle = self._begin(a)
        self.write_output(cycle, "plans/leftover/note.md", b"leftover\n")
        for route in (a, b, c):
            self._close_unproven(route)
        record = self._record(cycle)
        spent = self.P.RefreshBudget(float("inf"), float("inf"), 0.0)
        result = self.P.finalize(self.root, cycle_id=cycle["cycle_id"], state="abandoned",
                                 abandon_reason="route-unrecoverable", lock_timeout=0,
                                 exclude_symlinks=True, _scan_budget=spent)
        self.assertEqual((result["status"], result.get("reason")), ("deferred", "scan-budget"))
        self.assertEqual(self._record(cycle)["state"], "open")
        self.assertFalse((self.P.cycle_dir(self.root, record["campaign_id"], cycle["cycle_id"], record)
                          / "manifest.json").exists())
        # Without a time share the same close finishes.
        self.assertEqual(self.P.finalize(self.root, cycle_id=cycle["cycle_id"], state="abandoned",
                                         abandon_reason="route-unrecoverable", lock_timeout=0,
                                         exclude_symlinks=True)["status"], "sealed")

    def test_index_duplicate_shape_seals_on_begin_route(self):
        a = self._root_route("leftover-index")
        self._publish_root(a)
        b = self._continuation(a)
        first = self._begin(a)
        self.assertTrue(self.R.bind_continuation_cycle(self.root, a, b)["bound"])
        record = self._record(first)
        self.P._write_cycle_record(self.root, dict(record, state="abandoned"), exclusive=False)
        second = self._begin(b)
        self.P._write_cycle_record(self.root, record, exclusive=False)
        for cycle in (first, second):
            self.write_output(cycle, "plans/leftover/note.md", b"leftover\n")
        for route in (a, b):
            self._close_unproven(route)
        sealed = self.P.finalize(self.root, cycle_id=second["cycle_id"], state="abandoned",
                                 abandon_reason="route-unrecoverable")
        self.assertEqual(sealed["status"], "sealed")
        summary = self._sweep()
        self.assertEqual(self._results(summary), {first["cycle_id"]: "cycle-abandoned"})
        self.assertEqual(self._manifest_routes(first), [a["route_id"]])
        index = self.P.artifact_admission.load_index(self.root)
        root_id = next(iter(index.routes))
        self.assertEqual({route: row["cycle_id"] for route, row in index.routes[root_id].items()},
                         {a["route_id"]: first["cycle_id"], b["route_id"]: second["cycle_id"]})

    def test_symlink_is_left_out_and_the_cycle_closes_as_completed(self):
        route, route_file = self.route(slug="leftover-symlink", campaign_key="leftover-links")
        begun = self.P.begin(self.root, route_file=route_file, capability="autopilot-code",
                             intensity="direct", campaign_key="leftover-links")
        self.write_output(begun, "designs/mockup.html", b"<html></html>\n")
        link = Path(begun["cycle_dir"]) / "artifacts" / "designs" / "mockup-dark.html"
        link.symlink_to("mockup.html")
        self.close(route, route_file)   # proven: the completed seal is tried first
        summary = self._sweep()
        # §45 D-123: a link is not output, it never stopped a close; the proven route
        # closes as completed with the link on the exclusion list.
        self.assertEqual(self._results(summary), {begun["cycle_id"]: "cycle-completed"})
        record = self._record(begun)
        self.assertEqual(record["state"], "sealed")
        self.assertEqual(record["excluded_symlinks"], ["artifacts/designs/mockup-dark.html"])
        self.assertEqual(os.readlink(link), "mockup.html")
        directory = self.P.cycle_dir(self.root, record["campaign_id"], begun["cycle_id"], record)
        document = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual([row["locator"]["path"] for row in document["artifact_revisions"]],
                         ["artifacts/designs/mockup.html"])
        self.assertEqual(document["cycle"]["state"], "completed")

    def test_remembered_failure_retried_once_after_rule_change(self):
        route, route_file = self.route(slug="leftover-memory", campaign_key="leftover-memory")
        begun = self.P.begin(self.root, route_file=route_file, capability="autopilot-code",
                             intensity="direct", campaign_key="leftover-memory")
        self.write_output(begun, "reports/note.md", b"note\n")
        self._close_unproven(route)
        record = self._record(begun)
        self.P._write_cycle_record(self.root, dict(record, parent_cycle_id="cyc_" + "e" * 32), exclusive=False)
        state = self.root / ".runtime/route-autoclose/state.json"
        self.assertEqual(len(self._sweep()["cycles"]), 1)
        self.assertEqual(json.loads(state.read_text())["unsealable"][begun["cycle_id"]]["rules"], 2)
        self.assertEqual(self._sweep()["cycles"], [])   # remembered
        memory = json.loads(state.read_text())
        del memory["unsealable"][begun["cycle_id"]]["rules"]   # what an earlier version wrote
        state.write_text(json.dumps(memory))
        self.assertEqual(len(self._sweep()["cycles"]), 1)   # asked once more
        self.assertEqual(self._sweep()["cycles"], [])

    # -- empty workflow-group members ---------------------------------------
    def _grouped(self, key, count):
        """`count` open cycles in one campaign, each with a payload and an open manifest
        the workflow-group evidence binds to (as artifact_workflow_groups.test.py does)."""
        import artifact_workflow_groups as W
        if not self.P.artifact_lifecycle.read_root_identity(self.root):
            self.activate()
        rows = []
        for index in range(count):
            route, route_file = self.route(slug=f"{key}-{index}", campaign_key=key)
            begun = self.P.begin(self.root, route_file=route_file, capability="autopilot-code",
                                 intensity="direct", campaign_key=key)
            payload = self.write_output(begun, f"plans/{key}-{index}.md", f"cycle {index}\n".encode())
            artifact_id, revision_id = "art_" + f"{index + 1:032x}", "arev_" + f"{index + 1:032x}"
            interim = {
                "artifact_root_id": self.fixture.ROOT_ID, "repository_id": self.fixture.REPO_ID,
                "campaign": {"campaign_id": begun["campaign_id"]},
                "cycle": {"cycle_id": begun["cycle_id"], "campaign_id": begun["campaign_id"], "state": "open"},
                "artifacts": [{"artifact_id": artifact_id, "cycle_id": begun["cycle_id"]}],
                "artifact_revisions": [{"artifact_id": artifact_id, "artifact_revision_id": revision_id,
                                        "content_digest": W._digest(payload.read_bytes()),
                                        "locator": {"kind": "cycle-relative",
                                                    "path": f"artifacts/plans/{key}-{index}.md"}}],
            }
            manifest = self.P.producer_dir(self.root) / "open-manifests" / f"{begun['cycle_id']}.json"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(json.dumps(interim), encoding="utf-8")
            rows.append({"id": begun["cycle_id"], "campaign": begun["campaign_id"], "route": route,
                         "route_file": route_file, "file": payload,
                         "path": payload.relative_to(self.root).as_posix()})
        return rows

    @staticmethod
    def _group(title, rows, relations=()):
        return {"title": title,
                "members": [{"cycle_id": row["id"], "stage_label": f"Stage {n}"} for n, row in enumerate(rows)],
                "relations": [{"from_cycle_id": a["id"], "to_cycle_id": b["id"], "kind": "precedes",
                               "rationale": "The second cycle uses the first cycle's evidence.",
                               "evidence_refs": [{"path": a["path"]}, {"path": b["path"]}]}
                              for a, b in relations]}

    def _declare(self, campaign, *groups):
        import artifact_workflow_groups as W
        W.apply(self.root, W.prepare(self.root, campaign, {"groups": list(groups)}))
        return W

    def _empty_and_close(self, row):
        row["file"].unlink()
        row["file"].parent.rmdir()
        self._close_unproven(row["route"])

    def test_empty_member_withdrawn_and_recorded(self):
        import artifact_workflow_group_review as review
        rows = self._grouped("leftover-wg", 6)
        campaign = rows[0]["campaign"]
        W = self._declare(campaign,
                          self._group("Chain", rows[:3], [(rows[0], rows[1]), (rows[1], rows[2])]),
                          self._group("Alone", rows[3:4]),
                          self._group("Kept", rows[4:6], [(rows[4], rows[5])]))
        path = W.declaration_path(self.root, campaign)
        kept_bytes = W._bytes(W._load_existing(path)["groups"][2])
        self._empty_and_close(rows[1])    # ended empty inside this sweep
        self._empty_and_close(rows[3])
        started = time.monotonic()
        summary = self._sweep()
        print(f"route_autoclose_test withdrawal-sweep seconds={time.monotonic() - started:.3f}", file=sys.stderr)
        self.assertEqual(self._results(summary),
                         {rows[1]["id"]: "cycle-abandoned-empty", rows[3]["id"]: "cycle-abandoned-empty"})
        self.assertEqual([(row["campaign_id"], sorted(row["cycle_ids"]), row["groups_removed"])
                          for row in summary["withdrawn"]],
                         [(campaign, sorted([rows[1]["id"], rows[3]["id"]]), 1)])
        after = W._load_existing(path)
        self.assertEqual([group["title"] for group in after["groups"]], ["Chain", "Kept"])
        chain = after["groups"][0]
        self.assertEqual([item["cycle_id"] for item in chain["members"]], [rows[0]["id"], rows[2]["id"]])
        self.assertEqual(chain["relations"], [])
        self.assertEqual(W._bytes(after["groups"][1]), kept_bytes)
        self.assertEqual(W.verify(self.root, campaign)["groups"], 2)
        status, doc = review.read_record(self.root)
        self.assertEqual(status, "ok")
        entry = doc["cycles"][rows[1]["id"]]
        self.assertEqual((entry["verdict"], entry["mode"], entry["profile"], entry["cycle_state"]),
                         ("withdrawn-empty", "autoclose", None, "abandoned"))
        self.assertIn("no durable output", entry["reason"])
        self.assertEqual(entry["group_id"], chain["group_id"])
        self.assertEqual(entry["declaration_sha256"], W._digest(path.read_bytes()))
        # The second sweep changes nothing.
        before = (path.read_bytes(), path.stat().st_mtime_ns, json.dumps(doc, sort_keys=True))
        again = self._sweep()
        self.assertNotIn("withdrawn", again)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns,
                          json.dumps(review.read_record(self.root)[1], sort_keys=True)), before)
        # A member that already ended before the sweep, and a completed request that
        # found nothing (`no-lineage`), are taken on a later sweep.
        rows[2]["file"].unlink()
        rows[2]["file"].parent.rmdir()
        self.P.finalize(self.root, cycle_id=rows[2]["id"], state="abandoned", abandon_reason="route-unrecoverable")
        rows[5]["file"].unlink()
        rows[5]["file"].parent.rmdir()
        self.assertEqual(self.P.finalize(self.root, cycle_id=rows[5]["id"], state="completed")["status"],
                         "no-lineage")
        later = self._sweep()
        self.assertEqual(sorted(sum((row["cycle_ids"] for row in later["withdrawn"]), [])),
                         sorted([rows[2]["id"], rows[5]["id"]]))
        after = W._load_existing(path)
        self.assertEqual([group["title"] for group in after["groups"]], ["Chain", "Kept"])
        self.assertEqual([item["cycle_id"] for item in after["groups"][0]["members"]], [rows[0]["id"]])
        self.assertEqual([item["cycle_id"] for item in after["groups"][1]["members"]], [rows[4]["id"]])
        entry = review.read_record(self.root)[1]["cycles"][rows[5]["id"]]
        self.assertEqual((entry["verdict"], entry["cycle_state"]), ("withdrawn-empty", "no-lineage"))

    def test_withdrawal_problems_never_change_the_seal(self):
        import artifact_admission
        import artifact_workflow_groups as W
        plain = self._grouped("leftover-none", 2)         # no declaration at all
        broken = self._grouped("leftover-bad", 2)         # a declaration that is not JSON
        busy = self._grouped("leftover-busy", 2)          # the lock is held when it is due
        self._declare(broken[0]["campaign"], self._group("Bad", broken))
        self._declare(busy[0]["campaign"], self._group("Busy", busy))
        broken_path = W.declaration_path(self.root, broken[0]["campaign"])
        broken_path.write_bytes(b"{ not json")
        busy_path = W.declaration_path(self.root, busy[0]["campaign"])
        busy_before = busy_path.read_bytes()
        for rows in (plain, broken, busy):
            self._empty_and_close(rows[0])
        with mock.patch.object(W, "apply", side_effect=artifact_admission.AdmissionBusy("busy")):
            summary = self._sweep()
        self.assertEqual(self._results(summary), {rows[0]["id"]: "cycle-abandoned-empty"
                                                  for rows in (plain, broken, busy)})
        self.assertNotIn("withdrawn", summary)
        self.assertEqual(summary["errors"], [])
        self.assertEqual(broken_path.read_bytes(), b"{ not json")
        self.assertEqual(busy_path.read_bytes(), busy_before)
        # The busy one was not remembered: the next sweep applies it. The broken one waits
        # for its evidence to change.
        later = self._sweep()
        self.assertEqual([row["campaign_id"] for row in later["withdrawn"]], [busy[0]["campaign"]])
        self.assertEqual(broken_path.read_bytes(), b"{ not json")
        self.assertNotIn("withdrawn", self._sweep())


if __name__ == "__main__":
    unittest.main()
