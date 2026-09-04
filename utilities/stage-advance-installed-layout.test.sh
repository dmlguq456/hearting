#!/usr/bin/env bash
# stage-advance-installed-layout.test.sh -- SD-110 block 4 mandatory installed-layout
# reproduction (plan.md §8.3-2, checklist 4.6-4.8). Modelled on
# adapters/claude/bin/installed-layout-home.test.sh's bundle/source -> release ->
# current symlink chain, extended with a canonical jobs root, a REAL compiled
# route (autopilot-apply/dev/standard -- apply -> verify -> handback), and the
# REAL `stage-dispatch-fallback.py`, invoked through
# `dispatch_stage_advance.RealStageAdvanceServices` exactly the way the two
# session supervisors call it (not mocked -- "import만으로는 증거가 되지 않는다").
#
# Honest scope boundary (recorded, not silently narrowed): this sandbox has no
# live Claude/Codex harness credentials, so `start_successor`'s real subprocess
# call to `stage-dispatch-fallback.py --start` cannot actually spawn a child
# process. What IS proven end-to-end, for real, through the symlinked installed
# layout: (1) `close_gate` -- a real `capability-route.py complete` subprocess
# publishes a real completion marker and closes a real registry row; (2) `claim`
# -- a real `dispatch_contract.claim_stage_advance` CAS commits a real claim
# record under the real `<jobs>.lock`; (3) `start_successor` reaches the real
# wrapper (proving the same-argument-shape / no-precomputed-`--adapter` claim,
# §13.32.1-(3)A) and its real failure is caught and converted to the typed
# `stage-advance-successor-start-failed` refusal -- never a crash, never a
# silent hang, never a false advance. If the sandbox environment ever DOES have
# a live harness and the wrapper actually spawns a child, the ADVANCED branch is
# asserted instead -- this harness does not require failure, it requires a
# real, typed, non-crashing result either way.
#
# round 4 (⑤b) extends this with two more real, un-mocked I-tier halves that
# round 3 explicitly deferred: (4) A-15 -- SD-104's real
# `capability-route.build_continuation_route` computes `first_runnable_node`
# off the exact real marker/registry state phase 3 left behind (gate closed,
# successor NOT started), proving no `continuation-source-evidence-drift`
# misclassification; (5) A-5 -- a real, un-mocked PASS handoff with no
# declared artifact (`artifact: -`) drives the real envelope parser +
# `route_completion_evidence` into `stage-advance-evidence-unreadable`
# (P-tier's `EvidenceUnreadableTest` only mocks `gate_evidence` to that
# return value; this proves the real producer of it), on a second,
# independent jobs root so its claim/marker namespace never touches phase
# 3's already-committed claim for the same route_hash/successor pair. The
# live-harness successor-spawn half of A-1/A-13 stays exactly as unproven as
# round 3 left it (see above) -- neither round fabricates a passing
# assertion for that half.
set -u
REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

TMP=$(mktemp -d)
ARTIFACT_SCRATCH=""
cleanup() {
  rm -rf "$TMP"
  if [ -n "$ARTIFACT_SCRATCH" ]; then rm -rf "$ARTIFACT_SCRATCH"; fi
}
trap cleanup EXIT

fail() { printf 'FAIL - %s\n' "$1"; exit 1; }

if [ -n "$(find "$TMP" -name .git 2>/dev/null)" ]; then
  fail "fixture root unexpectedly contains .git"
fi

# --- installed layout: bundle/source -> release -> current (3-hop symlink
# chain, mirroring installed-layout-home.test.sh). Every hop ultimately points
# at THIS checkout, so `Path.resolve()` collapses back to the real repo -- the
# same behavior a real deployed `current` pointer has once it targets a
# concrete versioned release. -------------------------------------------------
mkdir -p "$TMP/bundle"
ln -s "$REPO_ROOT" "$TMP/bundle/source"
ln -s "$TMP/bundle/source" "$TMP/release"
mkdir -p "$TMP/home/.local/share/hearting"
ln -s "$TMP/release" "$TMP/home/.local/share/hearting/current"
WORKTREE="$TMP/home/.local/share/hearting/current"

if [ ! -f "$WORKTREE/core/CORE.md" ]; then
  fail "installed-layout symlink chain does not resolve to a valid AGENT_HOME (core/CORE.md missing)"
fi

# --- canonical jobs root (fresh, empty) --------------------------------------
mkdir -p "$TMP/jobs"
JOBS="$TMP/jobs/jobs.log"
: > "$JOBS"

# --- real canonical artifact root scratch space (artifact-root.sh resolves to
# the PRIMARY git worktree of this repo, not $TMP -- the terminal-envelope
# artifact must live there to read as "readable"/in-root). Cleaned up in the
# same trap as $TMP. -----------------------------------------------------
REAL_ARTIFACT_ROOT=$(env -u AGENT_ARTIFACT_ROOT "$REPO_ROOT/utilities/artifact-root.sh" "$REPO_ROOT" 2>/dev/null)
if [ -z "$REAL_ARTIFACT_ROOT" ]; then
  fail "artifact-root.sh could not resolve a root for $REPO_ROOT"
fi
ARTIFACT_SCRATCH="$REAL_ARTIFACT_ROOT/tmp/sd110-installed-layout-$$"
mkdir -p "$ARTIFACT_SCRATCH"
ARTIFACT_PATH="$ARTIFACT_SCRATCH/apply-artifact.md"
printf '# fixture artifact\n' > "$ARTIFACT_PATH"

RUNENV_HOME="$TMP/home"

echo "== phase 1: compile a REAL autopilot-apply/dev/standard route through the installed layout =="
DISPATCH_EVIDENCE="$TMP/dispatch-evidence.json"
python3 - "$WORKTREE" "$DISPATCH_EVIDENCE" <<'PYEOF'
import json, sys
worktree, out = sys.argv[1:3]


def tuple_row(child_harness):
    return {
        "child_harness": child_harness,
        "checked_worktree": worktree,
        "codex_command": "ok" if child_harness == "codex" else "not-applicable",
        "failure_class": "",
        "failure_scope": "none",
        "launch_authority": "conductor",
        "parent_harness": "claude",
        "parent_sandbox": "adapter-default",
        "parent_transport": "headless",
        "probe_source": "sd110-installed-layout-fixture",
        "probe_time": "2026-08-27T00:00:00Z",
        "status": "supported",
        "retry_on_isolated_worktree": 0,
    }


evidence = {
    "tuples": [tuple_row("claude"), tuple_row("codex"), tuple_row("opencode")],
    "native_subagent": [],
}
with open(out, "w", encoding="utf-8") as handle:
    json.dump(evidence, handle)
PYEOF

COMPILE_ERR="$TMP/compile.err"
env -u AGENT_HOME -u CLAUDE_HOME HOME="$RUNENV_HOME" AGENT_DISPATCH_JOBS="$JOBS" \
  python3 "$WORKTREE/utilities/capability-route.py" compile \
  --slug stage-advance-fixture \
  --capability autopilot-apply --capability-mode default --intensity standard \
  --cwd "$WORKTREE" --artifact-root "$TMP/route-artifact-root" \
  --tracking tracked --spec-read true --drift-verdict within-spec \
  --workflow-mode tracked --artifact-guard true \
  --dispatch-evidence "$DISPATCH_EVIDENCE" \
  >/dev/null 2>"$COMPILE_ERR"
compile_rc=$?
ROUTE_FILE=$(sed -n 's/^route_file=//p' "$COMPILE_ERR" | head -1)
if [ "$compile_rc" -ne 0 ] || [ -z "$ROUTE_FILE" ] || [ ! -s "$ROUTE_FILE" ]; then
  fail "compile: expected exit 0 and a written canonical route file, got rc=$compile_rc; stderr: $(cat "$COMPILE_ERR")"
fi
echo "ok   - compile: real route compiled through the symlinked installed layout"

echo "== phase 2+3: seal a predecessor row + run RealStageAdvanceServices end to end =="
env -u AGENT_HOME -u CLAUDE_HOME HOME="$RUNENV_HOME" AGENT_DISPATCH_JOBS="$JOBS" \
  python3 - "$ROUTE_FILE" "$JOBS" "$WORKTREE" "$ARTIFACT_PATH" "$TMP" "$REAL_ARTIFACT_ROOT" <<'PYEOF'
import json, os, subprocess, sys, time
from pathlib import Path

route_file, jobs, worktree, artifact_path, tmp, real_artifact_root = sys.argv[1:7]
fails = 0


def ok(label):
    print(f"ok   - {label}")


def bad(label, detail=""):
    global fails
    fails += 1
    print(f"FAIL - {label}" + (f": {detail}" if detail else ""))


route = json.loads(Path(route_file).read_text(encoding="utf-8"))
route_id = route["route_id"]
route_hash = route["route_hash"]

# --- phase 2: seal a predecessor ('apply') registry row + terminal envelope
log_dir = Path(tmp) / "logs"
log_dir.mkdir(parents=True, exist_ok=True)
log_file = log_dir / "apply.jsonl"
handoff = f"artifact: {artifact_path}\nverdict: PASS\nblocker: none"
log_file.write_text(
    json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": handoff}) + "\n",
    encoding="utf-8",
)

metadata = {
    "attempt_schema_version": "2",
    "dispatch_depth": "2",
    "transport": "headless",
    "execution_surface": "registered-headless",
    "registered_worker": "1",
    "fallback_hop": "same-harness-headless",
    "worker_type": "worker",
    "harness": "claude",
    "route_id": route_id,
    "route_hash": route_hash,
    "route_node": "apply",
    "attempt_id": "att-sd110-apply-fixture",
    "parent_attempt_id": "att-sd110-owner-fixture",
    "log_file": str(log_file),
    "artifact_root": real_artifact_root,
    "note": "completed-supervisor",
    "failure_class": "pass",
}
pipe = ",".join(f"{k}={v}" for k, v in metadata.items())
timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
line = "\t".join([timestamp, "done", worktree, worktree, "sd110-apply-fixture", pipe])
Path(jobs).write_text(line + "\n", encoding="utf-8")
ok("phase 2: predecessor 'apply' row + terminal envelope sealed")

# --- phase 3: RealStageAdvanceServices end to end, no mocks
ROOT = Path(worktree).resolve()
sys.path.insert(0, str(ROOT / "utilities"))
import dispatch_stage_advance as SA  # noqa: E402

request = SA.StageAdvanceRequest(
    jobs=Path(jobs),
    route_file=Path(route_file),
    predecessor_node="apply",
    predecessor_terminal_attempt_id="att-sd110-apply-fixture",
    parent_attempt_id="att-sd110-owner-fixture",
    supervisor_phase="parked",
    delivered_open_attempt_ids=frozenset(),
    receipt_schema_negotiated=3,
    harness="claude",
    worktree=worktree,
)
result = SA.coordinate_stage_advance(request, SA.RealStageAdvanceServices())
print(f"     result: outcome={result.outcome} reason={result.reason} "
      f"gate_closed={result.gate_closed} registered={result.registered} "
      f"started={result.started} child_spawned={result.child_spawned} "
      f"successor_node={result.successor_node}")

if result.gate_closed:
    ok("close_gate: real capability-route.py complete closed the gate")
else:
    bad("close_gate: gate was not closed", result.reason)

marker_path = SA.ROUTE.completion_dir(route_id, jobs=Path(jobs)) / "apply.json"
if marker_path.is_file():
    ok("close_gate: a real completion marker file exists on disk")
else:
    bad("close_gate: no completion marker file written", str(marker_path))

rows = Path(jobs).read_text(encoding="utf-8").splitlines()
if rows and "note=completed-marker" in rows[0]:
    ok("close_gate: the predecessor registry row was closed with note=completed-marker")
else:
    bad("close_gate: predecessor row was not marked completed-marker", rows[0] if rows else "<empty>")

if result.registered:
    claims_dir = Path(jobs).parent / "stage_advance" / "claims"
    claim_files = sorted(claims_dir.glob("*.json")) if claims_dir.is_dir() else []
    if claim_files:
        ok("claim: a real dispatch_contract.claim_stage_advance CAS record exists on disk")
    else:
        bad("claim: registered=True but no claim record file found under stage_advance/claims")
else:
    bad("claim: never reached (gate close likely failed upstream)", result.reason)

if result.successor_node == "verify":
    ok("classify_boundary: real route correctly identifies 'verify' as the unique runnable successor")
else:
    bad("classify_boundary: expected successor_node=verify", str(result.successor_node))

if result.outcome == "advanced":
    if result.child_spawned:
        ok("start_successor: a live harness in this sandbox actually spawned the successor")
    else:
        bad("start_successor: outcome=advanced but child_spawned is False")
elif result.outcome == "refused" and result.reason == "stage-advance-successor-start-failed":
    ok("start_successor: real stage-dispatch-fallback.py invocation failed cleanly "
       "(no live harness credentials in this sandbox) and was converted to a typed refusal, not a crash")
elif result.outcome == "refused" and result.registered:
    # Some other typed refusal after a successful claim is still an honest,
    # non-crashing, non-silent result -- record it, don't force a specific one.
    ok(f"start_successor: typed refusal '{result.reason}' after a real claim -- non-crashing, recorded")
else:
    bad("start_successor: unexpected outcome/reason combination", f"{result.outcome}/{result.reason}")

# --- replay: A-16 idempotence, proven for real (no injected checkpoints, a
# genuine second call against the same durable record).
result2 = SA.coordinate_stage_advance(request, SA.RealStageAdvanceServices())
if result2.stage_advance_id == result.stage_advance_id:
    ok("replay: a second coordinate_stage_advance call returns the identical stage_advance_id")
else:
    bad("replay: stage_advance_id drifted between calls",
        f"{result.stage_advance_id} != {result2.stage_advance_id}")
if result2.outcome == result.outcome and result2.reason == result.reason:
    ok("replay: outcome/reason are byte-identical on replay (no re-attempt, no drift)")
else:
    bad("replay: outcome/reason drifted between calls",
        f"{result.outcome}/{result.reason} != {result2.outcome}/{result2.reason}")

# --- phase 4 (A-15, ⑤b): SD-104's real `build_continuation_route` computes
# `first_runnable_node` off the SAME real marker/registry state phase 3 just
# left behind (gate closed, successor NOT started) -- no drift refusal, the
# resume point is exactly the un-started successor. Only meaningful when
# phase 3 actually reached a "gate closed, start refused" state (the only
# outcome this credential-less sandbox can produce); an ADVANCED phase 3
# skips this (there is no "successor start failed" state to resume from).
if result.gate_closed and result.outcome == "refused" and result.successor_node:
    import importlib.util as _ilu

    cr_spec = _ilu.spec_from_file_location(
        "sd110_capability_route_a15", Path(worktree) / "utilities" / "capability-route.py"
    )
    CR = _ilu.module_from_spec(cr_spec)
    cr_spec.loader.exec_module(CR)
    route_reloaded = json.loads(Path(route_file).read_text(encoding="utf-8"))
    try:
        continuation = CR.build_continuation_route(
            route_reloaded,
            resume_from_node=result.successor_node,
            requested_boundary=result.successor_node,
            reason="stage-advance-successor-start-failed",
            artifact_root=route_reloaded.get("artifact_root"),
        )
    except Exception as exc:  # a real, typed failure is still an honest result
        bad("A-15: build_continuation_route raised", f"{type(exc).__name__}: {exc}")
    else:
        if continuation.get("first_runnable_node") == result.successor_node:
            ok(
                "A-15: real SD-104 build_continuation_route computes "
                f"first_runnable_node={result.successor_node!r} off the real post-refusal state"
            )
        else:
            bad(
                "A-15: first_runnable_node mismatch",
                f"expected {result.successor_node!r}, got {continuation.get('first_runnable_node')!r}",
            )
        if continuation.get("first_runnable_blocker") is None:
            ok(
                "A-15: no continuation-source-evidence-drift or other blocker -- "
                "the real close_gate marker/registry state is fully continuation-ready"
            )
        else:
            bad(
                "A-15: unexpected first_runnable_blocker",
                str(continuation.get("first_runnable_blocker")),
            )
else:
    print(
        "note - A-15: skipped -- phase 3 did not leave a 'gate closed, start "
        "refused' state to resume from in this run "
        f"(outcome={result.outcome!r} reason={result.reason!r})"
    )

# --- phase 5 (A-5, ⑤b): one real `stage-advance-evidence-unreadable` case
# through the REAL envelope parser + `route_completion_evidence` (never
# mocked) -- a well-formed PASS handoff that declares no artifact
# (`artifact: -`) leaves `inspect_terminal_attempt`'s overall `state`
# "valid" (the envelope itself parsed fine) while `artifact_state` stays
# "none", the one real path into `route_completion_evidence`'s
# `evidence-not-readable` (P-tier's `EvidenceUnreadableTest` covers this
# exact mapping with `gate_evidence` mocked; this proves the real,
# un-mocked producer of that same reason string).
#
# The route's only other boundary (verify -> handback) is terminal-successor
# and refuses earlier (`stage-advance-terminal-node`) before evidence is even
# read, so this re-tests the SAME (apply -> verify) boundary the route
# supports -- but under a SECOND, independent jobs root (`jobs2`) so its
# claim/marker namespace never touches phase 3's already-committed
# `stage_advance_id` for that same route_hash/successor pair.
jobs2_dir = Path(tmp) / "jobs2"
jobs2_dir.mkdir(parents=True, exist_ok=True)
jobs2 = jobs2_dir / "jobs.log"
jobs2.touch()
verify_log = log_dir / "apply-unreadable.jsonl"
verify_handoff = "artifact: -\nverdict: PASS\nblocker: none"
verify_log.write_text(
    json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": verify_handoff}) + "\n",
    encoding="utf-8",
)
verify_metadata = dict(metadata)
verify_metadata.update({
    "attempt_id": "att-sd110-apply-unreadable-fixture",
    "log_file": str(verify_log),
})
verify_pipe = ",".join(f"{k}={v}" for k, v in verify_metadata.items())
jobs2.write_text(
    "\t".join([timestamp, "done", worktree, worktree, "sd110-apply-unreadable-fixture", verify_pipe]) + "\n",
    encoding="utf-8",
)

verify_request = SA.StageAdvanceRequest(
    jobs=jobs2,
    route_file=Path(route_file),
    predecessor_node="apply",
    predecessor_terminal_attempt_id="att-sd110-apply-unreadable-fixture",
    parent_attempt_id="att-sd110-owner-fixture",
    supervisor_phase="parked",
    delivered_open_attempt_ids=frozenset(),
    receipt_schema_negotiated=3,
    harness="claude",
    worktree=worktree,
)
verify_result = SA.coordinate_stage_advance(verify_request, SA.RealStageAdvanceServices())
print(f"     A-5 result: outcome={verify_result.outcome} reason={verify_result.reason} "
      f"gate_closed={verify_result.gate_closed}")
if verify_result.outcome == "refused" and verify_result.reason == "stage-advance-evidence-unreadable":
    ok("A-5: real envelope parser (artifact: - / valid PASS) produces "
       "stage-advance-evidence-unreadable through the un-mocked pipeline")
else:
    bad("A-5: expected outcome=refused reason=stage-advance-evidence-unreadable",
        f"{verify_result.outcome}/{verify_result.reason}")
if not verify_result.gate_closed:
    ok("A-5: gate close 0 on evidence-unreadable, as the reason table requires")
else:
    bad("A-5: gate_closed was True on an evidence-unreadable refusal")

# --- phase 6 (round-2 correction, impl-review finding F6): real
# installed-layout proof for the `stage-dispatch-fallback.py --start` typed
# `reason=` values `_classify_wrapper_start_failure` maps -- through the REAL
# wrapper subprocess and the REAL `capability-route.py verify` subprocess it
# execs internally (never a mocked `subprocess.run`), on JSON copies of the
# SAME real route phase 1 compiled. Only fields the wrapper itself
# real-checks are mutated, and each copy's `route_hash`/`route_id` is
# recomputed for real with `SA.ROUTE.route_hash` so the copy stays
# internally tamper-evident (never a route the wrapper would refuse for a
# reason OTHER than the one under test).
#
# Two of the three rows this round was asked to prove are exercised for real
# below: `stage-advance-launch-compatibility-mismatch` (wrapper reason
# `launch-compatibility-tuple-required`, decided by `capability-route.py
# verify --launch-phase start`'s `launch_compatibility_tuple` revalidation)
# and the generic-fallback row (wrapper reason `legacy-broker-route-read-only`,
# decided by the wrapper's own `dispatch_contract_version` gate). Both
# checks run BEFORE the wrapper's per-hop candidate loop, so proving them
# needs no live harness spawn and no credentials -- unlike A-1 above.
#
# The other two rows (`stage-advance-harness-unavailable` /
# `fallback-chain-exhausted`, and `stage-advance-lifecycle-unsupported` /
# `unsupported-native-execution-surface`) are NOT exercised for real here.
# This was verified empirically against the real wrapper in this same
# sandbox, not assumed: (a) `fallback-chain-exhausted` is the wrapper's
# trailing failure after its per-node hop loop runs out of hops, but the
# loop's mandatory last hop (`inline`, required by `load_node`'s own
# ORDER-equality check) unconditionally returns its own compile-time-fixed
# `reason=runtime-unavailable` first -- the trailing `fail(...)` call is
# unreachable through any `--start` invocation on a schema-valid route,
# independent of live credentials. (b) `unsupported-native-execution-surface`
# requires a `native-subagent` hop candidate whose harness has no
# `codex`/`claude` surface mapping, but `capability-route.py verify` recomputes
# each dispatch-depth-2 node's expected fallback chain from the route's own
# embedded `dispatch_evidence` and refuses the route if the node's
# `fallback_hops` differ (`fallback differs from checked evidence`), and that
# same evidence's own native-subagent normalizer rejects any harness outside
# {codex, claude} at compile time -- so no route that would pass real
# `verify` can carry the unsupported-harness candidate this reason needs.
# Both remain proven only at the unit level (`WrapperFailureClassificationTest`
# in dispatch_stage_advance.test.py, pure-classifier plus a mocked
# `subprocess.run`) -- a stated gap, not a faked green.
print("== phase 6: real wrapper subprocess proof for two typed start-failure reasons ==")
self_slug = os.environ.get("AGENT_DISPATCH_SELF_SLUG") or "att-sd110-owner-fixture"
verify_node = next(n for n in route["nodes"] if n["id"] == result.successor_node)
saved_dispatch_jobs_env = os.environ.get("AGENT_DISPATCH_JOBS")


def rehash(mutated):
    mutated["route_hash"] = SA.ROUTE.route_hash(mutated)
    mutated["route_id"] = "rt-" + mutated["route_hash"].split(":", 1)[1][:16]


def real_wrapper_case(mutated_route, label):
    """Run the REAL wrapper --start subprocess twice on the same mutated,
    rehashed route copy: once directly (to inspect its raw output fields --
    the `registered=`/`started=`/`child_spawned=` side-effect proof) and
    once through `RealStageAdvanceServices.start_successor` itself (to prove
    the classifier `_classify_wrapper_start_failure` actually sees and maps
    that same real output, not a stand-in)."""

    case_jobs = Path(tmp) / f"jobs-{label}" / "jobs.log"
    case_jobs.parent.mkdir(parents=True, exist_ok=True)
    case_jobs.touch()
    mutated_route_file = Path(tmp) / f"{label}.json"
    mutated_route_file.write_text(json.dumps(mutated_route), encoding="utf-8")
    os.environ["AGENT_DISPATCH_JOBS"] = str(case_jobs)
    raw_command = [
        sys.executable, str(SA.ROOT / "utilities" / "stage-dispatch-fallback.py"),
        "--route", str(mutated_route_file), "--node", verify_node["id"], "--slug", label,
        "--parent", self_slug, "--capability-mode", mutated_route.get("capability_mode") or "",
        "--worker-mode", verify_node.get("unit") or "", "--qa", "standard",
        "--model-role", "fast implementer", "--prompt-file", str(mutated_route_file),
        "--jobs", str(case_jobs), "--start",
    ]
    raw = subprocess.run(raw_command, text=True, capture_output=True)
    raw_fields = SA.FALLBACK.output_fields((raw.stdout or "") + (raw.stderr or ""))

    req = SA.StageAdvanceRequest(
        jobs=case_jobs, route_file=mutated_route_file, predecessor_node="apply",
        predecessor_terminal_attempt_id="att-sd110-apply-fixture",
        parent_attempt_id=self_slug, supervisor_phase="parked",
        delivered_open_attempt_ids=frozenset(), receipt_schema_negotiated=3,
        harness="claude", worktree=worktree,
    )
    claim = SA.StageAdvanceClaim(
        stage_advance_id=f"sadv-{label}", claim_key=(mutated_route["route_hash"], verify_node["id"], 0),
        successor_attempt_id=f"att-{label}-successor", replayed=False,
    )
    try:
        SA.RealStageAdvanceServices().start_successor(
            req, claim=claim, successor=verify_node, slug=label, prompt_file=mutated_route_file,
        )
    except SA.StageAdvanceError as exc:
        mapped_reason = exc.reason
    else:
        mapped_reason = None
    return raw_fields, mapped_reason


# --- case A: launch-compatibility-mismatch, real wrapper reason
# `launch-compatibility-tuple-required` (missing/legacy launch compatibility
# tuple -- the same real revalidation `capability-route.py verify
# --launch-phase start` performs for a live launch, never mocked).
route_mismatch = json.loads(json.dumps(route))
route_mismatch.pop("launch_compatibility_tuple", None)
rehash(route_mismatch)
mismatch_raw, mismatch_mapped = real_wrapper_case(route_mismatch, "sd110-f6-mismatch")
print(f"     case A raw wrapper fields: reason={mismatch_raw.get('reason')} "
      f"registered={mismatch_raw.get('registered')} started={mismatch_raw.get('started')} "
      f"child_spawned={mismatch_raw.get('child_spawned')}")
if mismatch_raw.get("reason") == "launch-compatibility-tuple-required":
    ok("case A: real wrapper subprocess emits the real "
       "reason=launch-compatibility-tuple-required for a missing launch compatibility tuple")
else:
    bad("case A: unexpected raw wrapper reason", str(mismatch_raw.get("reason")))
if (mismatch_raw.get("registered"), mismatch_raw.get("started"), mismatch_raw.get("child_spawned")) == ("0", "0", "0"):
    ok("case A: real wrapper output proves zero side effects "
       "(registered=0 started=0 child_spawned=0)")
else:
    bad("case A: expected registered=0 started=0 child_spawned=0", str(mismatch_raw))
if mismatch_mapped == "stage-advance-launch-compatibility-mismatch":
    ok("case A: RealStageAdvanceServices.start_successor classifies the real "
       "wrapper failure as stage-advance-launch-compatibility-mismatch")
else:
    bad("case A: expected mapped reason stage-advance-launch-compatibility-mismatch",
        str(mismatch_mapped))

# --- case B: an unclassified real wrapper reason (`legacy-broker-route-read-only`,
# from the wrapper's own `dispatch_contract_version` gate -- outside all three
# closed classification sets) still degrades to the generic
# stage-advance-successor-start-failed, proven through the real wrapper, not
# an injected string.
route_legacy = json.loads(json.dumps(route))
route_legacy["dispatch_contract_version"] = 1
rehash(route_legacy)
legacy_raw, legacy_mapped = real_wrapper_case(route_legacy, "sd110-f6-legacy")
print(f"     case B raw wrapper fields: reason={legacy_raw.get('reason')}")
if legacy_raw.get("reason") == "legacy-broker-route-read-only":
    ok("case B: real wrapper subprocess emits the real "
       "reason=legacy-broker-route-read-only for a downgraded dispatch_contract_version")
else:
    bad("case B: unexpected raw wrapper reason", str(legacy_raw.get("reason")))
if legacy_mapped == "stage-advance-successor-start-failed":
    ok("case B: an unclassified real wrapper reason still falls back to "
       "stage-advance-successor-start-failed, not a crash and not a silent drop")
else:
    bad("case B: expected mapped reason stage-advance-successor-start-failed",
        str(legacy_mapped))

if saved_dispatch_jobs_env is None:
    os.environ.pop("AGENT_DISPATCH_JOBS", None)
else:
    os.environ["AGENT_DISPATCH_JOBS"] = saved_dispatch_jobs_env

print(
    "note - F6: stage-advance-harness-unavailable (fallback-chain-exhausted) and "
    "stage-advance-lifecycle-unsupported (unsupported-native-execution-surface) stay "
    "unit-only-proven -- verified unreachable through any real --start invocation on a "
    "schema-valid route in this sandbox (see the phase 6 header comment above for the "
    "concrete reason), not a live-credential gap and not asserted here."
)

print("---")
if fails == 0:
    print("stage-advance-installed-layout.test.sh (python phase): PASS")
else:
    print(f"stage-advance-installed-layout.test.sh (python phase): FAIL ({fails})")
sys.exit(fails)
PYEOF
py_rc=$?

echo "---"
if [ "$py_rc" -eq 0 ]; then
  echo "stage-advance-installed-layout.test.sh: PASS"
else
  echo "stage-advance-installed-layout.test.sh: FAIL"
fi
exit "$py_rc"
