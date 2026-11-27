#!/usr/bin/env python3
"""2026-11-27: a stage the person selected at `top` that runs below top keeps going, and the lines that
already say how it ran carry `tier_note=selected top, ran <what ran>`.

Covered here: the shared rule (`model_profile.tier_note`), each way down in the stage launcher (another
harness, native-subagent, inline, an owner-inline repeat, a harness whose config declares no top), a
batch leg (started or reused) and its group ledger rows, `workflow-supervisor status` and the completion
summary the route's parent receives, which count each run once by the run id its writer knew: a
registered attempt (still read from its own ledger row once the archive moved it out of the registry),
the native attempt a `--start` confirmed (confirmed again, still one run), the owner that ran the stage
inline (two owners, two runs, even at one tier). A preview names no run. A stage not selected at top, or
one that ran a top model, prints what it printed before, and an observation that cannot be read never
changes the work. Fixtures never launch a model: the launcher's wrapper is a fixture or a real
`--dry-run`, and a real `--start` only confirms a native child (a private `sleep`) or hands over inline."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import contextlib
import errno
import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import model_profile as P  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The stage launcher's and the batch's own fixtures (route compile, registry parent, in-process chain;
# exact batch rows); only their helpers are bound below, so their tests do not run again here.
T = load("stage_dispatch_fallback_fixture", "utilities/stage_dispatch_fallback.test.py")
F = T.F
BT = load("dispatch_batch_fixture", "utilities/dispatch-batch.test.py")
BATCH = BT.BATCH
SUPERVISOR = load("workflow_supervisor_tier_note", "utilities/workflow-supervisor.py")
import work_intent  # noqa: E402
import dispatch_terminal_commit as COMMIT  # noqa: E402

OPENCODE_TOP = {"model_profile": "top", "model_tier": "balanced-deep",
                "profile_granularity": "collapsed-top-to-balanced-deep"}
# An older whole-file OpenCode copy: no top keys, and balanced-deep names its model itself.
OLD_OPENCODE_TOP = {"model_profile": "top", "model_tier": "model/opencode-go/muse-spark-1.3-contributor",
                    "profile_granularity": "collapsed-top-to-balanced-deep"}
NAMED_TOP = {"model_profile": "top", "model_tier": "model/fixture-named-top", "profile_granularity": "full"}
KEPT_TOP = {"model_profile": "top", "model_tier": "top", "profile_granularity": "full"}
DEEP = {"model_profile": "deep", "model_tier": "deep", "profile_granularity": "full"}


def setUpModule():
    T.setUpModule()
    BT.setUpModule()


def tearDownModule():
    BT.tearDownModule()
    T.tearDownModule()


def stage(node_id="impl-review", profile="top", kind="review-worker", depth=2):
    return {"id": node_id, "kind": kind, "dispatch_depth": depth, "model_profile": profile}


def supervisor_status(route, rows, jobs, *, as_json=True):
    """`workflow-supervisor status --route … [--json]` on ``route``, its registry ``rows`` (metadata dicts)
    and the ledger beside ``jobs``; the payload, or the text."""
    state = {"nodes": {}, "workflow_state": "CREATED", "updated_at": None}
    ledger = mock.Mock()
    ledger.read_only_state.return_value = state
    ledger.claims.return_value = []
    rows = [{"time": "now", "status": "done", "meta": meta} for meta in rows]
    with contextlib.ExitStack() as stack:
        for name, value in [("route_module", SimpleNamespace(resolve_route_argument=lambda *a: "route.json")),
                            ("load_route", route), ("ledger_for", ledger), ("read_armed", {}),
                            ("terminal_gate_state", {}), ("_closed_terminal_projection", False),
                            ("read_resource_watches", []), ("resource_children", []), ("ledger_metadata", {}),
                            ("_registry_rows", rows)]:
            stack.enter_context(mock.patch.object(SUPERVISOR, name, return_value=value))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            SUPERVISOR.cmd_status(SimpleNamespace(route="route.json", jobs=str(jobs), json=as_json))
    return json.loads(out.getvalue()) if as_json else out.getvalue()


def tier_lines(text):
    return [line for line in text.splitlines() if line.startswith("tier ")]


def config_text(adapter, *, drop=(), replace=None):
    """The shipped adapter config as a user's whole-file copy, minus ``drop`` keys, with ``replace``."""
    lines = []
    for line in (ROOT / "adapters" / adapter / "config" / "models.conf").read_text(encoding="utf-8").splitlines():
        key = line.split("=", 1)[0].strip()
        if key in drop:
            continue
        lines.append(f"{key}={replace[key]}" if replace and key in replace else line)
    return "\n".join(lines) + "\n"


class SharedRule(unittest.TestCase):
    def test_the_note_names_what_ran_and_only_below_top(self):
        top = stage()
        self.assertEqual(P.tier_note(top, harness="opencode", observed=OPENCODE_TOP),
                         "selected top, ran opencode balanced-deep")
        self.assertEqual(P.tier_note(top, harness="codex", surface="native"), "selected top, ran codex native-subagent")
        self.assertEqual(P.tier_note(top, surface="inline", harness="claude", observed=DEEP),
                         "selected top, ran inline (owner claude deep)")
        # A top model ran: the selected tier was kept, so nothing is said.
        for observed in (KEPT_TOP, NAMED_TOP):
            self.assertEqual(P.tier_note(top, harness="claude", observed=observed), "")
            self.assertEqual(P.tier_note(top, surface="inline", harness="claude", observed=observed), "")
        # Not a person-selected stage top: an ordinary stage, a depth-1 owner, a frame leg, a fan-out leg.
        for node in (stage(profile="deep"), stage(kind="capability-owner", depth=1), {**stage(), "worker_type": "frame",
                     "dispatch_depth": 1}, stage(kind="map-worker")):
            with self.subTest(node=node):
                self.assertEqual(P.tier_note(node, harness="opencode", observed=OPENCODE_TOP), "")
                self.assertEqual(P.tier_note(node, harness="codex", surface="native"), "")
                self.assertEqual(P.tier_note(node, surface="inline", harness="codex", observed={}), "")
        self.assertEqual(P.tier_note_ran("selected top, ran opencode balanced-deep"), "opencode balanced-deep")
        self.assertEqual(P.tier_note_ran("ran opencode"), "")

    def test_a_named_top_is_kept_and_a_folded_top_names_the_tier_it_ran(self):
        # The config's own words decide: a `collapsed-top-to-<tier>` top ran that tier whatever model it
        # names; `top` and a model the config names for top keep it; another profile ran that profile.
        self.assertEqual(P.ran_tier(OLD_OPENCODE_TOP), "balanced-deep")
        self.assertEqual(P.ran_tier(OPENCODE_TOP), "balanced-deep")
        self.assertEqual(P.ran_tier(NAMED_TOP), "")
        self.assertEqual(P.ran_tier(KEPT_TOP), "")
        self.assertEqual(P.ran_tier({"model_profile": "top", "model_tier": "deep", "profile_granularity": "full"}), "deep")
        self.assertEqual(P.ran_tier({"model_profile": "light", "model_tier": "model/fixture-light"}), "light")
        self.assertIsNone(P.ran_tier({}))
        self.assertEqual(P.tier_note(stage(), harness="opencode", observed=OLD_OPENCODE_TOP),
                         "selected top, ran opencode balanced-deep")
        self.assertEqual(P.tier_note(stage(), surface="inline", harness="opencode", observed=OLD_OPENCODE_TOP),
                         "selected top, ran inline (owner opencode balanced-deep)")
        # What was not observed is not guessed: no registered note, and an inline owner reads `tier unknown`.
        self.assertEqual(P.tier_note(stage(), harness="opencode", observed={}), "")
        self.assertEqual(P.tier_note(stage(), surface="inline", harness="codex", observed={}),
                         "selected top, ran inline (owner codex, tier unknown)")
        # An observation never raises.
        self.assertEqual(P.tier_note(stage(), harness="opencode", observed=object()), "")

    def test_the_shipped_adapter_configs_say_which_harness_runs_top_below_top(self):
        # OpenCode declares its top on the balanced-deep tier; an older whole-file copy without the key
        # collapses the same way, also when its balanced-deep names a model. Claude and Codex resolve top
        # to their top tier, and a copy naming a model for top keeps it.
        expected = {"claude": "", "codex": "", "opencode": "selected top, ran opencode balanced-deep"}
        for adapter, note in expected.items():
            with self.subTest(adapter=adapter):
                config = P.load_config(ROOT / "adapters" / adapter / "config" / "models.conf")
                resolved = P.resolve_profile_values(adapter, config, "top")
                observed = {"model_profile": resolved["profile"], "model_tier": resolved["tier"],
                            "profile_granularity": resolved["granularity"]}
                self.assertEqual(P.tier_note(stage(), harness=adapter, observed=observed), note)
        shipped = P.load_config(ROOT / "adapters/opencode/config/models.conf")
        for label, balanced_deep in (("shipped tier", None), ("named model", "model/opencode-go/fixture:xhigh")):
            with self.subTest(older=label):
                older = {key: value for key, value in shipped.items()
                         if not key.startswith("CFG_MODEL_PROFILE_TOP") and key != "CFG_MODEL_PROFILE_GRANULARITY_TOP"}
                if balanced_deep:
                    older["CFG_MODEL_PROFILE_BALANCED_DEEP"] = balanced_deep
                resolved = P.resolve_profile_values("opencode", older, "top")
                self.assertEqual(resolved["granularity"], "collapsed-top-to-balanced-deep")
                observed = {"model_profile": resolved["profile"], "model_tier": resolved["tier"],
                            "profile_granularity": resolved["granularity"]}
                self.assertEqual(P.tier_note(stage(), harness="opencode", observed=observed),
                                 "selected top, ran opencode balanced-deep")
        claude = {**P.load_config(ROOT / "adapters/claude/config/models.conf"),
                  "CFG_MODEL_PROFILE_TOP": "model/fixture-named-top:max"}
        resolved = P.resolve_profile_values("claude", claude, "top")
        self.assertEqual(P.tier_note(stage(), harness="claude", observed={
            "model_profile": resolved["profile"], "model_tier": resolved["tier"],
            "profile_granularity": resolved["granularity"]}), "")

    def test_the_route_reader_counts_each_run_once_by_the_run_id_its_writer_knew(self):
        # Review 3 (11-27): one rule, never record times -- a registered worker by its attempt, a native run by
        # the native attempt the launcher confirmed (`attempt_id`), an inline run by its owner (`parent_attempt_id`).
        route = {"route_id": "rt-a", "route_hash": "sha256:a",
                 "nodes": [stage("impl-review"), stage("code-review"), stage("plan", profile="deep", kind="pipeline-stage")]}
        row = {"route_id": "rt-a", "route_hash": "sha256:a", "route_node": "impl-review", "attempt_id": "att-1",
               "harness": "opencode", "launch_started": "1", **OPENCODE_TOP}
        rows = [{**row, "launch_started": "0"}, row, {**row, "note": "completed-marker"},  # one attempt, three rows
                {**row, "attempt_id": "att-2"},                                             # a retry: its own run
                {**row, "attempt_id": "att-top", "harness": "claude", **KEPT_TOP},         # ran top
                {**row, "attempt_id": "att-plan", "route_node": "plan"},                   # not selected
                {**row, "attempt_id": "att-other", "route_hash": "sha256:b"},              # another route
                {**row, "attempt_id": "att-unstarted", "launch_started": "0"},             # never ran
                # The native attempt's own registry row says nothing of a tier: its ledger row speaks for it.
                {"route_id": "rt-a", "route_hash": "sha256:a", "route_node": "impl-review", "attempt_id": "att-native",
                 "harness": "codex", "registered_worker": "0", "fallback_hop": "native-subagent"}]
        deep = "selected top, ran inline (owner claude deep)"
        inline = {"route_id": "rt-a", "route_hash": "sha256:a", "route_node": "impl-review", "fallback_hop": "inline",
                  "registered_worker": 0, "event_id": "dg-inline", "ts": 1.0, "parent_attempt_id": "att-owner-1",
                  "tier_note": deep}
        native = {**inline, "fallback_hop": "native-subagent", "event_id": "dg-native", "attempt_id": "att-native",
                  "tier_note": "selected top, ran codex native-subagent"}
        native.pop("parent_attempt_id")
        # A registered launch's own sole-gate row; its registry row says the same while it is there.
        sole_gate = {"route_id": "rt-a", "route_hash": "sha256:a", "route_node": "impl-review", "fallback_hop": None,
                     "reason": "sole-gate-non-peer-harness", "tier_note": "selected top, ran opencode balanced-deep"}
        ledger = [inline, {**inline, "ts": 2.0},             # the same owner confirms the stage inline again: one run
                  {**inline, "ts": 3.0, "parent_attempt_id": "att-owner-2"},  # same event id and tier, another owner
                  {**inline, "ts": 4.0, "parent_attempt_id": "att-owner-3",
                   "tier_note": "selected top, ran inline (owner claude balanced-deep)"},
                  native, {**native, "ts": 5.0},              # the same native attempt confirmed again: one run
                  {**native, "attempt_id": "att-native-2"},   # another native attempt: its own run
                  {**sole_gate, "attempt_id": "att-1"},       # the registry already counted att-1
                  {**sole_gate, "attempt_id": "att-archived"}, {**sole_gate, "attempt_id": "att-archived", "ts": 6.0},
                  {**inline, "route_node": "code-review", "ts": 7.0},  # the same owner, another stage: its own run
                  # Rows naming no run -- a preview's, or one written before rows named their run.
                  sole_gate, {**inline, "parent_attempt_id": None}, {key: value for key, value in native.items()
                                                                     if key != "attempt_id"},
                  {**inline, "tier_note": None},
                  {**inline, "route_hash": "sha256:b"}]
        expected = [
            {"node": "impl-review", "attempt_id": "att-1", "tier_note": "selected top, ran opencode balanced-deep"},
            {"node": "impl-review", "attempt_id": "att-2", "tier_note": "selected top, ran opencode balanced-deep"},
            {"node": "impl-review", "fallback_hop": "inline", "tier_note": deep},
            {"node": "impl-review", "fallback_hop": "inline", "tier_note": deep},
            {"node": "impl-review", "fallback_hop": "inline",
             "tier_note": "selected top, ran inline (owner claude balanced-deep)"},
            {"node": "impl-review", "fallback_hop": "native-subagent", "tier_note": "selected top, ran codex native-subagent"},
            {"node": "impl-review", "fallback_hop": "native-subagent", "tier_note": "selected top, ran codex native-subagent"},
            {"node": "impl-review", "attempt_id": "att-archived", "tier_note": "selected top, ran opencode balanced-deep"},
            {"node": "code-review", "fallback_hop": "inline", "tier_note": deep},
        ]
        self.assertEqual(P.top_stage_tier_notes(route, rows=rows, degradations=ledger), expected)
        # The whole ledger read twice is the same runs.
        self.assertEqual(P.top_stage_tier_notes(route, rows=rows, degradations=ledger + ledger), expected)
        plain = {**route, "nodes": [stage("impl-review", profile="deep")]}
        self.assertEqual(P.top_stage_tier_notes(plain, rows=rows, degradations=ledger), [])


class StageLauncher(unittest.TestCase):
    """The stage fallback chain on the fixture's `execute` stage (and `plan`, `plan-check` for real dry runs),
    sealed `top` by the person at strong. A chain is `--start` unless it says `--dry-run`."""
    # The fallback fixture's own setup and helpers.
    setUp = T.FallbackTest.setUp
    tearDown = T.FallbackTest.tearDown
    seed_parent = T.FallbackTest.seed_parent
    fixture_subprocess_env = T.FallbackTest.fixture_subprocess_env
    tuple = T.FallbackTest.tuple
    cli_verdict = T.FallbackTest.cli_verdict
    launch_roots_env = T.FallbackTest.launch_roots_env
    route = T.FallbackTest.route
    seed_predecessor_markers = T.FallbackTest.seed_predecessor_markers
    seed_plan_marker = T.FallbackTest.seed_plan_marker
    dispatch_env = T.FallbackTest.dispatch_env
    degradation_rows = T.FallbackTest.degradation_rows
    run_chain = T.FallbackTest.run_chain
    NATIVE_ATTEMPT = "att-native-fixture"

    OPENCODE = ("check=ok\nadapter=opencode\nstatus=start\nmodel_source=profile\nmodel_profile=top\n"
                "model_tier=balanced-deep\nprofile_granularity=collapsed-top-to-balanced-deep\nmodel=fixture-model\n")
    CLAUDE_TOP = "check=ok\nadapter=claude\nstatus=start\nmodel_profile=top\nmodel_tier=top\nmodel=fixture-model\n"
    REFUSED = "check=failed\nreason=fixture-runtime-refusal\n"
    UNDECLARED = "check=failed\nreason=profile-top-undeclared\nchild_spawned=0\n"
    INLINE = "selected top, ran inline (owner codex deep)"

    def seed_owner(self, observed, attempt=T.FALLBACK_PARENT):
        """The one live depth-1 owner row (generation ``attempt``), carrying what its own launch resolved
        (``observed``)."""
        start = (Path("/proc") / str(self.owner.pid) / "stat").read_text().split()[21]
        extra = "".join(f",{key}={value}" for key, value in observed.items())
        kept = [line for line in (self.jobs.read_text().splitlines() if self.jobs.exists() else ())
                if "worker_type=owner" not in line]
        self.jobs.write_text("".join(line + "\n" for line in kept) + (
            f"2026-07-23T00:00:00Z\topen\t{self.repo}\t{self.repo}\towner\t"
            "attempt_schema_version=2,dispatch_depth=1,transport=headless,harness=codex,"
            "runtime_sandbox=workspace-write,execution_surface=registered-headless,registered_worker=1,"
            "fallback_hop=same-harness-headless,worker_type=owner,"
            f"attempt_id={attempt},pid={self.owner.pid},pid_start={start}{extra}\n"), encoding="utf-8")

    def launch(self, path, action):
        """The launcher's own `_dispatch` in this process on `execute` (the fallback fixture's `run_inline`, with
        ``action``; a `--start` takes the launch watch as observed)."""
        self.seed_predecessor_markers(path, "execute")
        argv = ["stage-dispatch-fallback.py", "--route", str(path), "--node", "execute", "--slug", "fallback-execute",
                "--parent", "owner", "--capability-mode", "dev", "--jobs", str(self.jobs), f"--{action}"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(F, "watch_launched_attempt", return_value=("observed", {})):
            return F._dispatch(F.LAUNCH_TUPLE.ReportOnlyObservation())

    def cli(self, path, node, action, *extra, owner=T.FALLBACK_PARENT, **env):
        """The real `stage-dispatch-fallback.py --<action>` process on ``node`` for owner generation ``owner``."""
        command = [sys.executable, str(ROOT / "utilities/stage-dispatch-fallback.py"), "--route", str(path),
                   "--node", node, "--slug", f"fallback-{node}", "--parent", "owner", "--capability-mode", "dev",
                   "--jobs", str(self.jobs), f"--{action}", *extra]
        inherited = {key: value for key, value in self.fixture_subprocess_env().items()
                     if not key.startswith("AGENT_DISPATCH_CURRENT_")}
        return subprocess.run(command, text=True, capture_output=True, env={
            **inherited, **self.launch_roots_env(), "AGENT_DISPATCH_SELF_SLUG": "owner",
            "AGENT_DISPATCH_ATTEMPT_ID": owner, **env})

    @staticmethod
    def every_tuple_failed(route, node_id):
        """`--failed-tuple` for every headless candidate of ``node_id``: the chain goes on to native, then inline."""
        node = next(node for node in route["nodes"] if node["id"] == node_id)
        return [value for hop in node["fallback_hops"] for row in hop.get("candidates", []) if row.get("child_harness")
                for value in ("--failed-tuple", "/".join(row[key] for key in (
                    "parent_harness", "parent_transport", "parent_sandbox", "child_harness", "launch_authority")))]

    def native_attempt(self, route, attempt):
        """A live route-owned native attempt on `execute` (its registry row, a private live pid)."""
        process = subprocess.Popen(["sleep", "60"])
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        start = (Path("/proc") / str(process.pid) / "stat").read_text().split()[21]
        with self.jobs.open("a", encoding="utf-8") as handle:
            handle.write(f"2026-07-16T00:00:00Z\topen\t/repo\t{self.repo}\tnative\tattempt_schema_version=2,"
                         f"route_id={route['route_id']},route_node=execute,attempt_id={attempt},dispatch_depth=2,"
                         "transport=headless,harness=codex,execution_surface=codex-native-subagent,registered_worker=0,"
                         f"fallback_hop=native-subagent,pid={process.pid},pid_start={start}\n")
        return attempt

    def hop(self, ordinal, kind, harness=None):
        if kind == "inline":
            return {"ordinal": ordinal, "fallback_hop": "inline", "reason_enum": "runtime-unavailable"}
        if kind == "native":
            return {"ordinal": ordinal, "fallback_hop": "native-subagent",
                    "candidates": [{"harness": harness, "status": "supported"}]}
        return {"ordinal": ordinal, "fallback_hop": kind, "candidates": [self.tuple(harness, "supported")]}

    def chain(self, profile, hops, outputs, *, proof=None, patches=(), owner=DEEP, owner_attempt=T.FALLBACK_PARENT,
              action="start", unreadable_after_hops=False, context=None):
        """One in-process chain on `execute`; returns (exit, stdout, ledger rows this chain wrote, route).

        ``owner`` is what the owner's registry row (generation ``owner_attempt``) says its launch resolved.
        ``proof`` proves `NATIVE_ATTEMPT` at a native hop. With ``unreadable_after_hops`` every read of the
        registry fails with EIO once the hops are ordered. ``context`` is the allocation verdict the ordering
        returns beside the hops."""
        with self.dispatch_env(AGENT_DISPATCH_ATTEMPT_ID=owner_attempt):
            path = self.route(same_status="supported", explicit_profiles={"execute": profile} if profile else None)
            self.seed_owner(owner, owner_attempt)
            before = len(self.degradation_rows(path))
            results = [SimpleNamespace(returncode=0 if text.startswith("check=ok") else 64, stdout=text, stderr="")
                       for text in outputs]
            ordered = []

            def order(*_args, **_kwargs):
                ordered.append(True)
                return hops, context

            jobs, read_text, path_open = self.jobs, Path.read_text, Path.open

            def unreadable(original):
                def read(self_path, *args, **kwargs):
                    if ordered and unreadable_after_hops and Path(self_path) == jobs:
                        raise OSError(errno.EIO, "fixture registry read failure", str(self_path))
                    return original(self_path, *args, **kwargs)
                return read

            out = io.StringIO()
            with mock.patch.object(F, "ordered_fallback_hops", side_effect=order), \
                 mock.patch.object(F, "native_child_proof",
                                   return_value=(proof, self.NATIVE_ATTEMPT) if proof else ("", "")), \
                 mock.patch.object(F, "run_wrapper", side_effect=results), \
                 mock.patch.object(Path, "read_text", unreadable(read_text)), \
                 mock.patch.object(Path, "open", unreadable(path_open)), contextlib.ExitStack() as stack, \
                 contextlib.redirect_stdout(out):
                for patch in patches:
                    stack.enter_context(patch)
                code = self.launch(path, action)
            return code, out.getvalue(), self.degradation_rows(path)[before:], json.loads(path.read_text())

    @staticmethod
    def own_block(stdout):
        """The launcher's own lines (a relayed wrapper receipt follows the second `check=`)."""
        lines = stdout.splitlines()
        starts = [index for index, line in enumerate(lines) if line.startswith("check=")]
        return lines[:starts[1]] if len(starts) > 1 else lines

    def test_each_way_down_says_what_was_selected_and_what_ran_and_the_work_goes_on(self):
        cases = (
            # path, hops, wrapper outputs, native proof, exit, receipt note, ledger reason (None: no ledger row)
            ("another harness: OpenCode resolves top to balanced-deep",
             [self.hop(1, "cross-harness-headless", "opencode")], [self.OPENCODE], None, 0,
             "selected top, ran opencode balanced-deep", None),
            ("native-subagent: the native catalog has no top",
             [self.hop(1, "same-harness-headless", "codex"), self.hop(2, "native", "codex")], [self.REFUSED],
             "fixture-native-proof", 78, "selected top, ran codex native-subagent", "native-subagent-degraded"),
            ("inline: the owner's own tier",
             [self.hop(1, "same-harness-headless", "codex"), self.hop(2, "inline")], [self.REFUSED], None, 79,
             self.INLINE, "runtime-unavailable"),
            ("profile-top-undeclared: the harness is refused and the chain goes down",
             [self.hop(1, "cross-harness-headless", "claude"), self.hop(2, "inline")], [self.UNDECLARED], None, 79,
             self.INLINE, "runtime-unavailable"),
        )
        for label, hops, outputs, proof, expected_code, note, reason in cases:
            with self.subTest(path=label):
                code, out, ledger, route = self.chain("top", hops, outputs, proof=proof)
                execute = next(node for node in route["nodes"] if node["id"] == "execute")
                self.assertEqual((execute["model_profile"], route["effective_intensity"]), ("top", "strong"))
                self.assertEqual(code, expected_code, out)
                own = self.own_block(out)
                # One note, on the receipt's own lines, right beside the check it qualifies.
                self.assertEqual(own.count(f"tier_note={note}"), 1, out)
                self.assertEqual(own[0], "check=ok" if code == 0 else "check=degraded")
                if code:
                    self.assertEqual(own[1], f"tier_note={note}")
                else:
                    self.assertEqual(own[own.index("child_harness=opencode") + 1], f"tier_note={note}")
                # The ledger row names its run: the native attempt the launcher proved, or the owner inline.
                noted = [(row["reason"], row["tier_note"], row.get("attempt_id") or row.get("parent_attempt_id"))
                         for row in ledger if "tier_note" in row]
                run = self.NATIVE_ATTEMPT if proof else T.FALLBACK_PARENT
                self.assertEqual(noted, [] if reason is None else [(reason, note, run)])
                if "undeclared" in label:
                    self.assertEqual(json.loads(self.cli_verdict(out)["candidate_outcomes"])[0]["reason"],
                                     "profile-top-undeclared")

    def test_an_owner_inline_repeat_of_a_top_stage_says_the_owner_ran_it(self):
        escalation = {"kind": "owner-inline", "from": "top", "launch_profile": "top", "streak": 2,
                      "class": "scope", "attempts": ["a", "b"]}
        code, out, ledger, _route = self.chain(
            "top", [self.hop(1, "same-harness-headless", "codex"), self.hop(2, "inline")], [],
            patches=(mock.patch("model_profile.repeat_escalation", return_value=escalation),))
        self.assertEqual(code, 79, out)
        self.assertIn("repeat_escalation=owner-inline\n", out)
        self.assertIn(f"check=degraded\ntier_note={self.INLINE}\nselected_hop=inline\n", out)
        self.assertIn("reason=repeat-failure\n", out)
        self.assertEqual([(row["reason"], row["tier_note"]) for row in ledger if "tier_note" in row],
                         [("repeat-failure", self.INLINE)])

    def test_the_inline_owner_tier_is_the_owners_own_launch_never_the_routes_request(self):
        hops = [self.hop(1, "same-harness-headless", "codex"), self.hop(2, "inline")]
        cases = (
            # owner row, note (None: no note)
            ("a top model ran", KEPT_TOP, None),
            ("a model named for top ran", NAMED_TOP, None),
            ("an older OpenCode-style folded top ran", OLD_OPENCODE_TOP, "selected top, ran inline (owner codex balanced-deep)"),
            # The fixture route asks `deep` for its owner; an unrecorded tier is not reported as that.
            ("the owner row records no tier", {}, "selected top, ran inline (owner codex, tier unknown)"),
        )
        for label, owner, note in cases:
            with self.subTest(owner=label):
                code, out, ledger, route = self.chain("top", hops, [self.REFUSED], owner=owner)
                self.assertEqual(route["owner_model_profile"], "deep")
                self.assertEqual(code, 79, out)
                if note is None:
                    self.assertNotIn("tier_note", out)
                    self.assertFalse([row for row in ledger if "tier_note" in row])
                else:
                    self.assertIn(f"check=degraded\ntier_note={note}\nselected_hop=inline\n", out)
                    self.assertEqual([row["tier_note"] for row in ledger if "tier_note" in row], [note])

    def test_an_unreadable_registry_at_the_inline_hop_still_hands_the_stage_over(self):
        # The inline receipt names the owner's tier from the parent row resolved at the start: nothing is
        # read again, so a registry that fails from here on still gives 79 and the same hand-over lines.
        hops = [self.hop(1, "inline")]
        for profile in ("top", None):
            with self.subTest(stage_profile=profile or "route default"):
                code, out, _ledger, _route = self.chain(profile, hops, [])
                failing, failed_out, _ledger, _route = self.chain(profile, hops, [], unreadable_after_hops=True)
                self.assertEqual(code, 79, out)
                self.assertEqual((failing, failed_out), (code, out))
                self.assertIn("selected_hop=inline\n", failed_out)
                self.assertIn("route_reuse=required\n", failed_out)
                self.assertEqual("tier_note=" in out, profile == "top")

    @staticmethod
    def sole_gate(harness):
        """An allocation verdict with no quality-peer family left: the launched worker is a sole-gate one."""
        return {"strategy": "balanced", "window": 30, "counts": dict.fromkeys(F.ALLOCATION_HARNESSES, 0),
                "rank": [harness], "sole_gate": "degraded"}

    def every_reader(self, route, *, twice=True):
        """What the reader, `status --json`, the status text and the seal's reader (the parent's summary)
        each say about ``route``, from this fixture's registry and the whole route ledger (read twice)."""
        rows = [row["meta"] for row in SUPERVISOR._registry_rows(self.jobs)]
        ledger = self.degradation_rows(self.current_route_file)
        request = SimpleNamespace(jobs=str(self.jobs), owner_attempt_id=T.FALLBACK_PARENT)
        return (P.top_stage_tier_notes(route, rows=rows, degradations=ledger + ledger if twice else ledger),
                supervisor_status(route, rows, self.jobs).get("tier_notes"),
                tier_lines(supervisor_status(route, rows, self.jobs, as_json=False)),
                COMMIT._tier_notes(request, route))

    def test_two_owners_running_a_top_stage_inline_and_a_native_run_are_three_runs_everywhere(self):
        # Review 2 (11-27): a later owner generation runs the stage inline again. The inline row's event id
        # names the stage and its hop, not the run, so both rows carry the same one; each names its owner and
        # is its own run, and so is a native-subagent run of the same stage.
        inline = [self.hop(1, "same-harness-headless", "codex"), self.hop(2, "inline")]
        native = [self.hop(1, "same-harness-headless", "codex"), self.hop(2, "native", "codex")]
        balanced = {"model_profile": "balanced-deep", "model_tier": "balanced-deep", "profile_granularity": "full"}
        written = []
        for hops, owner, generation, proof, expected_code in (
                (inline, DEEP, "att-owner-first", None, 79), (inline, balanced, "att-owner-later", None, 79),
                (native, DEEP, "att-owner-later", "fixture-native-proof", 78)):
            code, out, ledger, route = self.chain("top", hops, [self.REFUSED], owner=owner, owner_attempt=generation,
                                                  proof=proof)
            self.assertEqual(code, expected_code, out)
            written += [row for row in ledger if "tier_note" in row]
        notes = [self.INLINE, "selected top, ran inline (owner codex balanced-deep)",
                 "selected top, ran codex native-subagent"]
        self.assertEqual([(row["tier_note"], row.get("parent_attempt_id"), row.get("attempt_id")) for row in written],
                         [(notes[0], "att-owner-first", None), (notes[1], "att-owner-later", None),
                          (notes[2], None, self.NATIVE_ATTEMPT)])
        self.assertEqual(written[0]["event_id"], written[1]["event_id"])
        expected = [{"node": "execute", "fallback_hop": hop, "tier_note": note}
                    for hop, note in zip(("inline", "inline", "native-subagent"), notes)]
        self.assertEqual(self.every_reader(route, twice=False), self.every_reader(route))
        self.assertEqual(self.every_reader(route),
                         (expected, expected, [f"tier       execute {note}" for note in notes], expected))
        with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "en"}):
            line = work_intent._tier_note_line(self.every_reader(route)[3], work_intent.ui_translator(), 1600)
        self.assertEqual(line, "Ran below the selected tier: execute (selected top, ran inline (owner codex deep) / "
                               "inline (owner codex balanced-deep) / codex native-subagent)")

    def test_a_started_launch_is_one_run_while_its_registry_row_lives_and_after_the_archive_moves_it(self):
        # Review 2 (11-27): the launch's own sole-gate row stands aside while the registry has counted that
        # attempt; once the archive moves the attempt's rows out, the ledger row still says what ran.
        import dispatch_registry_archive as ARCHIVE
        note = "selected top, ran opencode balanced-deep"
        code, out, ledger, route = self.chain("top", [self.hop(1, "cross-harness-headless", "opencode")],
                                              [self.OPENCODE], context=self.sole_gate("opencode"))
        self.assertEqual(code, 0, out)
        attempt = self.cli_verdict(out)["attempt_id"]
        sole = [row for row in ledger if row.get("reason") == "sole-gate-non-peer-harness"]
        self.assertEqual([(row["route_node"], row["attempt_id"], row["tier_note"]) for row in sole],
                         [("execute", attempt, note)])
        # The row `--start` registers for this launch, from what its wrapper resolved; the attempt has ended.
        meta = {"attempt_schema_version": "2", "dispatch_depth": "2", "registered_worker": "1",
                "route_id": route["route_id"], "route_hash": route["route_hash"], "route_node": "execute",
                "attempt_id": attempt, "worker_type": "stage", "launch_started": "1", "harness": "opencode",
                **OPENCODE_TOP}
        with self.jobs.open("a", encoding="utf-8") as handle:
            handle.write(f"2026-11-01T00:00:00Z\tdone\t{self.repo}\t{self.repo}\tfallback-execute\t"
                         + ",".join(f"{key}={value}" for key, value in meta.items()) + "\n")
        expected = [{"node": "execute", "attempt_id": attempt, "tier_note": note}]
        # One run, while the registry row and its sole-gate row are both there (review 1's double).
        live = self.every_reader(route)
        self.assertEqual(live, (expected, expected, [f"tier       execute {note}"], expected))
        # The archive moves the ended attempt's row (the fixture decides it qualifies; the move is real).
        moved = lambda lines, **_kwargs: [index for index, line in enumerate(lines) if attempt in line]
        with mock.patch.object(ARCHIVE, "select", side_effect=moved):
            result = ARCHIVE.sweep(self.jobs, force=True)
        self.assertEqual(result.get("removed"), 1, result)
        self.assertNotIn(attempt, self.jobs.read_text())
        self.assertTrue(ARCHIVE.archived_rows(self.jobs, attempt))
        self.assertEqual(self.every_reader(route), live)

    def test_a_sole_gate_row_with_no_note_or_from_a_preview_is_written_as_before(self):
        deep = self.OPENCODE.replace("model_profile=top", "model_profile=deep")
        for profile, harness, output, action in ((None, "opencode", deep, "start"),
                                                 ("top", "claude", self.CLAUDE_TOP, "start"),
                                                 ("top", "opencode", self.OPENCODE, "dry-run")):
            with self.subTest(stage_profile=profile or "route default", harness=harness, action=action):
                code, out, ledger, _route = self.chain(profile, [self.hop(1, "cross-harness-headless", harness)],
                                                       [output], context=self.sole_gate(harness), action=action)
                self.assertEqual(code, 0, out)
                sole = [row for row in ledger if row.get("reason") == "sole-gate-non-peer-harness"]
                self.assertEqual(len(sole), 1, ledger)
                self.assertFalse({"attempt_id", "tier_note"} & set(sole[0]), sole)

    def test_a_real_preview_names_no_run_and_no_reader_says_it_ran(self):
        # Review 3 (11-27): a real `--dry-run` of a top `plan-check` with OpenCode alone in a user's deep
        # last-resort seat, under a codex owner: the real OpenCode wrapper previews (nothing registers or
        # starts), yet the sole-gate row named the previewed attempt and every reader said it ran. The preview
        # receipt keeps its note and the sole-gate row stays the diagnostic it was; no reader names a run.
        shipped = (ROOT / "profiles/dispatch-defaults.yaml").read_text(encoding="utf-8")
        band = "  deep:\n    primary: [claude, codex]\n    relief: []\n    last_resort: []\n"
        self.assertEqual(shipped.count(band), 1)
        parent = {"AGENT_DISPATCH_CURRENT_HARNESS": "codex", "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
                  "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write"}
        note = "selected top, ran opencode balanced-deep"
        last_resort = band.replace("last_resort: []", "last_resort: [opencode]")
        with T.dispatch_defaults_config_text(shipped.replace(band, last_resort)):
            path = self.route(tuples=[self.tuple(harness, "unsupported") for harness in ("codex", "claude")]
                              + [self.tuple("opencode", "supported")], explicit_profiles={"plan-check": "top"})
            route = json.loads(path.read_text())
            self.seed_owner(DEEP)
            self.seed_plan_marker(route)
            self.seed_predecessor_markers(path, "plan-check")
            registry = self.jobs.read_bytes()
            result = self.cli(path, "plan-check", "dry-run", "--worker-mode", "qa/plan-review",
                              "--model-role", "fast reviewer", **parent)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        own = self.own_block(result.stdout)
        relayed = result.stdout.splitlines()[len(own):]
        self.assertIn("sole_gate=degraded", own)
        self.assertEqual(own[own.index("child_harness=opencode") + 1], f"tier_note={note}")
        for line in ("preview=1", "registered=0", "started=0", "child_spawned=0"):
            self.assertIn(line, relayed)
        self.assertEqual(self.jobs.read_bytes(), registry)
        sole = [row for row in self.degradation_rows(path) if row.get("reason") == "sole-gate-non-peer-harness"]
        self.assertEqual(len(sole), 1, sole)
        self.assertFalse({"attempt_id", "tier_note"} & set(sole[0]), sole)
        self.assertEqual(self.every_reader(route), ([], None, [], []))

    def test_the_same_native_run_confirmed_twice_is_one_run_and_another_native_run_is_its_own(self):
        # Review 3 (11-27): real `--start` confirmed one native attempt (its id, a live private pid) twice; the
        # two ledger rows share the event id and differ only in time, and were counted as two runs. A preview
        # names no run.
        path = self.route(native="supported", explicit_profiles={"execute": "top"})
        route = json.loads(path.read_text())
        self.seed_owner(DEEP)
        self.seed_predecessor_markers(path, "execute")
        failed = self.every_tuple_failed(route, "execute")
        note = "selected top, ran codex native-subagent"
        first, other = self.native_attempt(route, "att-native-first"), self.native_attempt(route, "att-native-other")
        for action, attempt in (("dry-run", first), ("start", first), ("start", first)):
            result = self.cli(path, "execute", action, *failed, "--native-attempt-id", attempt)
            self.assertEqual(result.returncode, 78, result.stdout + result.stderr)
            self.assertIn(f"check=degraded\ntier_note={note}\nselected_hop=native-subagent\n", result.stdout)
            self.assertIn("child_proof=registry-exact-pid\n", result.stdout)
        rows = [row for row in self.degradation_rows(path) if row.get("reason") == "native-subagent-degraded"]
        self.assertEqual([(row.get("attempt_id"), row.get("tier_note")) for row in rows],
                         [(None, None), (first, note), (first, note)])
        self.assertEqual(rows[1]["event_id"], rows[2]["event_id"])
        self.assertNotEqual(rows[1]["ts"], rows[2]["ts"])
        one = [{"node": "execute", "fallback_hop": "native-subagent", "tier_note": note}]
        self.assertEqual(self.every_reader(route), (one, one, [f"tier       execute {note}"], one))
        # Another native attempt, proven by its route-owned artifact, then by its id: one more run.
        artifact = Path(self.tmp.name) / "native-other.json"
        artifact.write_text(json.dumps({"route_id": route["route_id"], "route_hash": route["route_hash"],
                                        "route_node": "execute", "producer_attempt_id": other}), encoding="utf-8")
        for proof, value, source in (("--native-artifact", str(artifact), "route-owned-artifact"),
                                     ("--native-attempt-id", other, "registry-exact-pid")):
            result = self.cli(path, "execute", "start", *failed, proof, value)
            self.assertEqual(result.returncode, 78, result.stdout + result.stderr)
            self.assertIn(f"child_proof={source}\n", result.stdout)
        self.assertEqual([row.get("attempt_id") for row in self.degradation_rows(path)
                          if row.get("reason") == "native-subagent-degraded"][3:], [other, other])
        self.assertEqual(self.every_reader(route), (one * 2, one * 2, [f"tier       execute {note}"] * 2, one * 2))

    def test_two_owners_running_a_top_stage_inline_at_the_same_tier_are_two_runs(self):
        # Review 3 (11-27): two owners of the same `deep` tier run the stage inline: the same event id and the
        # same note, two runs. The same owner confirming it again is the same run; a preview names no run.
        path = self.route(explicit_profiles={"execute": "top"})
        route = json.loads(path.read_text())
        self.seed_predecessor_markers(path, "execute")
        failed = self.every_tuple_failed(route, "execute")
        for action, owner in (("dry-run", "att-owner-a"), ("start", "att-owner-a"), ("start", "att-owner-b"),
                              ("start", "att-owner-a")):
            self.seed_owner(DEEP, owner)
            result = self.cli(path, "execute", action, *failed, owner=owner)
            self.assertEqual(result.returncode, 79, result.stdout + result.stderr)
            self.assertIn(f"check=degraded\ntier_note={self.INLINE}\nselected_hop=inline\n", result.stdout)
        rows = [row for row in self.degradation_rows(path) if row.get("fallback_hop") == "inline"]
        self.assertEqual([(row.get("parent_attempt_id"), row.get("tier_note")) for row in rows],
                         [(None, None), ("att-owner-a", self.INLINE), ("att-owner-b", self.INLINE),
                          ("att-owner-a", self.INLINE)])
        self.assertEqual(len({row["event_id"] for row in rows}), 1)
        two = [{"node": "execute", "fallback_hop": "inline", "tier_note": self.INLINE}] * 2
        self.assertEqual(self.every_reader(route), (two, two, [f"tier       execute {self.INLINE}"] * 2, two))

    def test_no_note_where_top_was_not_selected_or_a_top_model_ran_and_the_bytes_are_unchanged(self):
        cases = (
            ("top ran top on claude", "top", [self.hop(1, "cross-harness-headless", "claude")], [self.CLAUDE_TOP], None),
            ("default profile on opencode", None, [self.hop(1, "cross-harness-headless", "opencode")],
             [self.OPENCODE.replace("model_profile=top", "model_profile=deep")], None),
            ("default profile, native", None,
             [self.hop(1, "same-harness-headless", "codex"), self.hop(2, "native", "codex")], [self.REFUSED],
             "fixture-native-proof"),
            ("default profile, inline", None,
             [self.hop(1, "same-harness-headless", "codex"), self.hop(2, "inline")], [self.REFUSED], None),
        )
        strip = lambda rows: [{key: value for key, value in row.items() if key not in {"ts", "event_id"}}
                              for row in rows]
        for label, profile, hops, outputs, proof in cases:
            with self.subTest(case=label):
                code, out, ledger, route = self.chain(profile, hops, outputs, proof=proof)
                if profile is None:
                    self.assertNotEqual(next(n for n in route["nodes"] if n["id"] == "execute")["model_profile"], "top")
                self.assertNotIn("tier_note", out)
                self.assertFalse([row for row in ledger if "tier_note" in row], ledger)
                # The same chain with the note switched off -- what the launcher printed before it existed.
                with mock.patch.object(F, "tier_note", return_value=""):
                    again, before, ledger_before, _ = self.chain(profile, hops, outputs, proof=proof)
                self.assertEqual((code, out), (again, before))
                self.assertEqual(strip(ledger), strip(ledger_before))

    def widened(self):
        # The shipped dispatch defaults keep OpenCode out of the deep band a `top` stage borrows, so a top
        # stage reaches OpenCode only through a user band, a harness pin or an OpenCode owner.
        shipped = (ROOT / "profiles/dispatch-defaults.yaml").read_text(encoding="utf-8")
        band = "  deep:\n    primary: [claude, codex]\n"
        self.assertEqual(shipped.count(band), 1)
        return T.dispatch_defaults_config_text(shipped.replace(band, "  deep:\n    primary: [claude, codex, opencode]\n"))

    def test_a_real_dry_run_on_a_harness_band_that_admits_opencode_resolves_top_to_balanced_deep(self):
        # A user band admits OpenCode; the real OpenCode `--dry-run` wrapper resolves the shipped top and the
        # chain says so. A Claude top resolves to its top tier and the chain says nothing.
        for harness, note in (("opencode", "tier_note=selected top, ran opencode balanced-deep\n"), ("claude", None)):
            with self.subTest(harness=harness), self.widened():
                path = self.route(tuples=[self.tuple("codex", "unsupported"), self.tuple(harness, "supported")],
                                  explicit_profiles={"plan": "top"})
                result = self.run_chain(path)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(f"child_harness={harness}\n", result.stdout)
                self.assertIn("model_profile=top\n", result.stdout)
                if note:
                    self.assertIn("model_tier=balanced-deep\n", result.stdout)
                    self.assertIn(f"child_harness={harness}\n{note}", result.stdout)
                else:
                    self.assertIn("model_tier=top\n", result.stdout)
                    self.assertNotIn("tier_note", result.stdout)

    def test_real_dry_runs_under_a_users_own_model_config(self):
        # The person's whole-file copies, read by the real wrappers: a Claude copy that declares no top is
        # refused by the wrapper itself and the chain goes on to OpenCode; an older OpenCode copy (no top
        # keys, a balanced-deep that names its model) folds top onto balanced-deep; a Claude copy that names
        # a model for top keeps top.
        old_opencode = config_text("opencode", drop={"CFG_MODEL_PROFILE_TOP", "CFG_MODEL_PROFILE_GRANULARITY_TOP"},
                                   replace={"CFG_MODEL_PROFILE_BALANCED_DEEP":
                                            "model/opencode-go/muse-spark-1.3-contributor:xhigh"})
        cases = (
            # label, tuples, user configs, child, lines that must appear, note
            ("claude declares no top", ("claude", "opencode"),
             {"claude": config_text("claude", drop={"CFG_MODEL_PROFILE_TOP", "CFG_MODEL_PROFILE_GRANULARITY_TOP"})},
             "opencode", ("model_tier=balanced-deep\n",), "selected top, ran opencode balanced-deep"),
            ("an older opencode copy", ("opencode",), {"opencode": old_opencode}, "opencode",
             ("model_tier=model/opencode-go/muse-spark-1.3-contributor\n",
              "profile_granularity=collapsed-top-to-balanced-deep\n"), "selected top, ran opencode balanced-deep"),
            ("claude names a model for top", ("claude",),
             {"claude": config_text("claude", replace={"CFG_MODEL_PROFILE_TOP": "model/fixture-named-top:max"})},
             "claude", ("model_tier=model/fixture-named-top\n",), None),
        )
        for label, harnesses, configs, child, lines, note in cases:
            with self.subTest(config=label), self.widened(), tempfile.TemporaryDirectory() as home:
                env = {"HEARTING_MODEL_CONFIG_HOME": "", "CLAUDE_CONFIG_DIR": str(Path(home) / "claude"),
                       "XDG_CONFIG_HOME": str(Path(home) / "xdg")}
                for adapter, text in configs.items():
                    target = (Path(env["CLAUDE_CONFIG_DIR"]) if adapter == "claude"
                              else Path(env["XDG_CONFIG_HOME"]) / "opencode") / "agent-config" / "models.conf"
                    target.parent.mkdir(parents=True)
                    target.write_text(text, encoding="utf-8")
                path = self.route(tuples=[self.tuple("codex", "unsupported"),
                                          *(self.tuple(harness, "supported") for harness in harnesses)],
                                  explicit_profiles={"plan": "top"})
                result = self.run_chain(path, **env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                verdict = self.cli_verdict(result.stdout)
                self.assertEqual(verdict["child_harness"], child, result.stdout)
                for line in lines:
                    self.assertIn(line, result.stdout)
                if label == "claude declares no top":
                    refused = [item for item in json.loads(verdict["candidate_outcomes"]) if item.get("attempted")]
                    self.assertEqual(refused[0]["reason"], "invalid-dispatch-model-profile", result.stdout)
                if note:
                    self.assertIn(f"child_harness={child}\ntier_note={note}\n", result.stdout)
                else:
                    self.assertNotIn("tier_note", result.stdout)


class BatchLeg(unittest.TestCase):
    def result(self, node, observed):
        leg = {"node": "impl-review", "adapter": "opencode", "attempt_id": "att-leg", "model_profile": "top"}
        stdout = ("check=ok\nadapter=opencode\nstatus=start\nattempt_id=att-leg\nregistered=1\nstarted=1\n"
                  "child_spawned=1\nduplicate_attempt=0\nmodel=fixture\n"
                  + "".join(f"{key}={value}\n" for key, value in observed.items()))
        return BATCH.wrapper_result(leg, SimpleNamespace(returncode=0), stdout, "", node=node)

    def test_a_selected_top_leg_that_ran_below_top_carries_the_note_and_nothing_else_changes(self):
        for observed in (OPENCODE_TOP, OLD_OPENCODE_TOP):
            with self.subTest(tier=observed["model_tier"]):
                noted = self.result(stage(), observed)
                self.assertEqual(noted["launch_state"], "started")
                self.assertEqual(noted["tier_note"], "selected top, ran opencode balanced-deep")
                baseline = self.result(None, observed)
                self.assertNotIn("tier_note", baseline)
                self.assertEqual({key: value for key, value in noted.items() if key != "tier_note"}, baseline)
        for node, observed in ((stage(), KEPT_TOP), (stage(), NAMED_TOP), (stage(profile="deep"), OPENCODE_TOP)):
            with self.subTest(node=node["model_profile"], tier=observed["model_tier"]):
                self.assertEqual(self.result(node, observed), self.result(None, observed))


class BatchReuse(unittest.TestCase):
    """A leg the batch reuses as it is (`existing-*`) says what its exact attempt ran, from its registry row."""
    setUp = BT.DispatchBatchTest.setUp
    common_patches = BT.DispatchBatchTest.common_patches
    legs = BT.DispatchBatchTest.legs
    write_existing = BT.DispatchBatchTest.write_existing

    def reuse(self, profile, observed):
        self.route["nodes"][0] = {**self.route["nodes"][0], "kind": "review-worker", "model_profile": profile}
        legs = self.legs()
        self.write_existing(legs[0], status="running", all_legs=legs, append=False)
        line = self.jobs.read_text(encoding="utf-8").rstrip("\n")
        self.jobs.write_text(line + "".join(f",{key}={value}" for key, value in observed.items()) + "\n",
                             encoding="utf-8")
        meta = BATCH.parse_registry_metadata(line.split("\t", 5)[5])
        return BATCH.existing_leg_result(
            self.jobs, legs[0], self.route, repo=str(self.base), parent="owner", parent_attempt_id=BT.PARENT_FIXTURE,
            parallel_group="plan", declared_size=2, manifest_digest=meta["batch_manifest_sha256"],
            leg_digest=meta["batch_leg_sha256"], agent_home=self.base)

    def test_a_reused_live_attempt_carries_the_note_its_start_did(self):
        noted = self.reuse("top", OPENCODE_TOP)
        self.assertEqual((noted["launch_state"], noted["reason"]), ("existing", "existing-active-attempt"))
        self.assertEqual(noted["tier_note"], "selected top, ran codex balanced-deep")
        for profile, observed in (("top", KEPT_TOP), ("top", NAMED_TOP), ("balanced-deep", OPENCODE_TOP)):
            with self.subTest(profile=profile, tier=observed["model_tier"]):
                result = self.reuse(profile, observed)
                self.assertEqual(result["reason"], "existing-active-attempt")
                self.assertNotIn("tier_note", result)
        self.assertEqual({key: value for key, value in noted.items() if key != "tier_note"},
                         self.reuse("top", KEPT_TOP))

    def test_the_group_ledger_rows_carry_the_noted_leg_and_keep_their_group_meaning(self):
        noted = {"launch_state": "started", "attempt_id": "att-top", "tier_note": "selected top, ran claude balanced-deep"}
        plain = {"launch_state": "started", "attempt_id": "att-peer"}
        diagnostics = {"degradation_cause": "single-usable-family", "degradation_detail": "usable=claude",
                       "sole_gate": "degraded"}
        calls = {}
        for label, results in (("noted", [noted, plain]), ("reused", [{**noted, "launch_state": "existing"}, plain]),
                               ("plain", [plain, {**plain, "attempt_id": "att-two"}])):
            with mock.patch.object(BATCH, "record_degradation") as record:
                BATCH._persist_launched_degradation(self.base, self.route, diagnostics, results)
                BATCH._persist_sole_gate_degradation(self.base, self.route, diagnostics, results)
            calls[label] = [call.kwargs for call in record.call_args_list]
        self.assertEqual([kwargs["reason"] for kwargs in calls["noted"]],
                         ["single-usable-family", "sole-gate-non-peer-harness"])
        for kwargs in calls["noted"] + calls["reused"]:
            self.assertIsNone(kwargs["route_node"])
            self.assertEqual((kwargs["tier_note"], kwargs["attempt_id"]),
                             ("selected top, ran claude balanced-deep", "att-top"))
        # Without a noted leg the two rows are written exactly as before.
        self.assertEqual([{key: value for key, value in kwargs.items() if key not in {"tier_note", "attempt_id"}}
                          for kwargs in calls["noted"]], calls["plain"])
        self.assertFalse([kwargs for kwargs in calls["plain"] if "tier_note" in kwargs or "attempt_id" in kwargs])
        # The real ledger keeps the field, and a group row is not read back as a second run.
        BATCH._persist_sole_gate_degradation(self.base, self.route, diagnostics, [noted, plain])
        import dispatch_degradation
        rows = dispatch_degradation.route_rows(self.route["route_id"], agent_home=self.base, jobs=self.jobs)
        self.assertEqual([(row["route_node"], row["reason"], row["attempt_id"], row["tier_note"]) for row in rows],
                         [(None, "sole-gate-non-peer-harness", "att-top", "selected top, ran claude balanced-deep")])
        route = {**self.route, "nodes": [stage("plan")]}
        self.assertEqual(P.top_stage_tier_notes(route, degradations=rows), [])


class WhereThePersonReads(unittest.TestCase):
    """`workflow-supervisor status --json` and the completion summary the route's parent receives."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jobs = Path(self.tmp.name) / "jobs.log"
        self.jobs.write_text("")
        self.route = {"route_id": "rt-fixture", "route_hash": "sha256:fixture", "capability": "autopilot-code",
                      "nodes": [stage("impl-review"), stage("plan", profile="deep", kind="pipeline-stage")]}
        self.inline = {"route_id": "rt-fixture", "route_hash": "sha256:fixture", "route_node": "impl-review",
                       "fallback_hop": "inline", "registered_worker": 0, "parent_attempt_id": "att-owner",
                       "tier_note": "selected top, ran inline (owner claude deep)"}
        # The same launch's sole-gate row: it repeats what the registry row below already says.
        self.sole_gate = {"route_id": "rt-fixture", "route_hash": "sha256:fixture", "route_node": "impl-review",
                          "fallback_hop": None, "reason": "sole-gate-non-peer-harness", "sole_gate": "degraded",
                          "tier_note": "selected top, ran opencode balanced-deep"}
        self.ledger = self.jobs.parent / "degradations" / "rt-fixture.jsonl"
        self.ledger.parent.mkdir()
        self.ledger.write_text("".join(json.dumps(row) + "\n" for row in (self.inline, self.sole_gate)))
        self.row = {"route_id": "rt-fixture", "route_hash": "sha256:fixture", "route_node": "impl-review",
                    "attempt_id": "att-leg", "harness": "opencode", "launch_started": "1", **OPENCODE_TOP}

    def status(self, route, rows, *, as_json=True):
        return supervisor_status(route, rows, self.jobs, as_json=as_json)

    def test_status_names_each_run_once_and_only_runs_below_a_selected_top(self):
        rows = [{**self.row, "launch_started": "0"}, self.row, {**self.row, "note": "completed-marker"},
                {**self.row, "attempt_id": "att-top", "harness": "claude", **KEPT_TOP}]
        payload = self.status(self.route, rows)
        self.assertEqual(payload["tier_notes"], [
            {"node": "impl-review", "attempt_id": "att-leg", "tier_note": "selected top, ran opencode balanced-deep"},
            {"node": "impl-review", "fallback_hop": "inline", "tier_note": "selected top, ran inline (owner claude deep)"},
        ])
        text = self.status(self.route, rows, as_json=False)
        self.assertEqual([line for line in text.splitlines() if line.startswith("tier ")], [
            "tier       impl-review selected top, ran opencode balanced-deep",
            "tier       impl-review selected top, ran inline (owner claude deep)"])
        # A retry is its own run.
        retried = self.status(self.route, [self.row, {**self.row, "attempt_id": "att-retry"}])
        self.assertEqual([item.get("attempt_id") for item in retried["tier_notes"]], ["att-leg", "att-retry", None])
        # No selected-top stage, or one that ran top: the key and the line are absent, as before.
        plain = {**self.route, "nodes": [stage("impl-review", profile="deep")]}
        self.assertNotIn("tier_notes", self.status(plain, [self.row]))
        self.assertNotIn("tier ", self.status(plain, [self.row], as_json=False))
        self.ledger.write_text("")
        self.assertNotIn("tier_notes", self.status(self.route, [{**self.row, **KEPT_TOP}]))

    def summary(self, notes, *, primary=None, language="en", **kwargs):
        report = Path(primary or Path(self.tmp.name) / "final_report.md")
        if not report.exists():
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("## 5. Unverified\nnone\n")
        with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": language}):
            return work_intent.completion_summary(self.route, str(report), t=work_intent.ui_translator(),
                                                  tier_notes=notes, **kwargs)

    def test_the_completion_summary_has_one_line_in_the_persons_language(self):
        notes = P.top_stage_tier_notes(self.route, rows=[self.row, {**self.row, "attempt_id": "att-retry"}],
                                       degradations=[self.inline])
        for language, line in (
                ("en", "Ran below the selected tier: impl-review (selected top, ran opencode balanced-deep ×2 / "
                       "inline (owner claude deep))"),
                ("ko", "고른 등급보다 낮게 돈 단계: impl-review (고른 등급 top · 실제 opencode balanced-deep ×2 / "
                       "inline (owner claude deep))")):
            with self.subTest(language=language):
                with_notes = self.summary(notes, language=language)
                without = self.summary(None, language=language)
                self.assertIn(line + "\n", with_notes)
                self.assertEqual(with_notes.replace(line + "\n", ""), without)
                self.assertEqual(self.summary([], language=language), without)

    def test_the_tier_line_takes_only_the_room_the_summary_leaves(self):
        many = [{"node": f"stage-{index:02d}", "attempt_id": f"att-{index}",
                 "tier_note": "selected top, ran opencode balanced-deep"} for index in range(40)]
        for language, more in (("en", "+{} more"), ("ko", "외 {}")):
            with self.subTest(language=language):
                base = self.summary(None, language=language)
                # Many stages: whole items, then the rest counted; the artifact line stays whole.
                folded = self.summary(many, language=language)
                tier = next(line for line in folded.splitlines() if "stage-00" in line)
                shown = tier.count("stage-")
                self.assertTrue(0 < shown < 40, tier)
                self.assertTrue(tier.endswith("); " + more.format(40 - shown)), tier)
                self.assertLessEqual(len(folded.rstrip("\n")), work_intent.MAX_SUMMARY_CHARS)
                self.assertEqual(folded.replace(tier + "\n", ""), base)
                # A long artifact path: the summary without the line is the same bytes as before, and the line
                # never pushes the path out -- it shrinks or steps aside.
                deep = Path(self.tmp.name, *(f"{language}-{'d' * 180}-{index}" for index in range(7)), "final_report.md")
                long_base = self.summary(None, primary=deep, language=language)
                self.assertIn(str(deep), long_base)
                for notes in (many[:1], many):
                    with_notes = self.summary(notes, primary=deep, language=language)
                    self.assertIn(str(deep) + "\n", with_notes)
                    lines = [line for line in with_notes.splitlines() if "stage-" in line]
                    self.assertEqual(with_notes.replace(lines[0] + "\n", "") if lines else with_notes, long_base)
                # A summary already at the limit has no room: it is left exactly as it was.
                room = work_intent.MAX_SUMMARY_CHARS - len(long_base.rstrip("\n")) - 10
                self.assertTrue(0 < room < 255, room)
                fuller = Path(deep.parent, "e" * room, "final_report.md")
                full_base = self.summary(None, primary=fuller, language=language)
                self.assertGreaterEqual(len(full_base.rstrip("\n")), work_intent.MAX_SUMMARY_CHARS - 10)
                self.assertEqual(self.summary(many[:1], primary=fuller, language=language), full_base)
                self.assertIn(str(fuller), full_base)

    def test_the_seal_reads_the_routes_registry_rows_and_ledger(self):
        fields = ",".join(f"{key}={value}" for key, value in self.row.items())
        self.jobs.write_text(f"2026-11-27T00:00:00Z\tdone\t/work\t/work\tleg\t{fields}\n")
        request = SimpleNamespace(jobs=str(self.jobs), owner_attempt_id="att-owner")
        self.assertEqual(COMMIT._tier_notes(request, self.route), [
            {"node": "impl-review", "attempt_id": "att-leg", "tier_note": "selected top, ran opencode balanced-deep"},
            {"node": "impl-review", "fallback_hop": "inline", "tier_note": "selected top, ran inline (owner claude deep)"},
        ])
        plain = {**self.route, "nodes": [stage("impl-review", profile="deep")]}
        self.assertEqual(COMMIT._tier_notes(request, plain), [])

    def test_the_sealed_summary_the_parent_receives_carries_the_line(self):
        fields = ",".join(f"{key}={value}" for key, value in self.row.items())
        self.jobs.write_text(f"2026-11-27T00:00:00Z\tdone\t/work\t/work\tleg\t{fields}\n")
        report = Path(self.tmp.name) / "final_report.md"
        report.write_text("## 5. Unverified\nnone\n")
        request = SimpleNamespace(route_file=None, jobs=str(self.jobs), owner_attempt_id="att-owner")
        sealed = {}
        for label, route in (("selected top", self.route),
                             ("no selected top", {**self.route, "nodes": [stage("impl-review", profile="deep")]})):
            slot = Path(self.tmp.name) / label.replace(" ", "-")
            slot.mkdir()
            with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "en"}):
                COMMIT._seal_completion_summary(request, {**route, "cwd": self.tmp.name}, str(report), slot)
            sealed[label] = (slot / work_intent.SUMMARY_NAME).read_text()
        line = ("Ran below the selected tier: impl-review (selected top, ran opencode balanced-deep / "
                "inline (owner claude deep))\n")
        self.assertIn(line, sealed["selected top"])
        self.assertNotIn("selected tier", sealed["no selected top"])
        self.assertEqual(sealed["selected top"].replace(line, ""), sealed["no selected top"])


if __name__ == "__main__":
    unittest.main()
