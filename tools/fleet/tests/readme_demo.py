"""Public README demo for the Fleet Explorer: English, neutral, synthetic.

The same shape ``explorer_fixture.demo_snapshot`` has (``Session`` / ``DispatchJob`` rows projected
the way a live tick projects them, plus compute-host rows), with names a public reader can read:
fictional projects, host names and paths, English "now" lines, and one fixed version string.
Nothing here reads the home directory, the attempt registry, herdr or the network; nothing is a
real session. README captures built from it say so ("demo data").

``snapshot()`` is what ``explorer_capture.py --readme-demo`` and ``readme_captures.py`` draw.
``VERSION`` is the one string to bump when the README is re-captured for a new release.
"""
import atexit
import dataclasses
import json
import os
import shutil
import tempfile
import time

VERSION = "v4.32.1"

PROJECTS = ("web-api", "ml-train", "docs-site", "mobile-app")
HOME = "/home/demo"
# 12:00 UTC on a fixed day: header clock and "N ago" columns do not depend on when the capture runs.
NOW = 1_793_620_800.0                  # 2026-11-02 12:00:00 UTC; a past instant, so "result at <= now" holds

_NODE_KEYS = ("id", "kind", "depth", "depends_on", "role", "write_scope", "resource_class",
              "completion_gate")


def _route(capability, mode, cwd, title, nodes):
    """A sealed, hash-consistent route record (``route.load`` rejects a hand-written hash)."""
    from tools.fleet import route as routemod
    rows = []
    for node_id, depends, role in nodes:
        rows.append({"id": node_id, "kind": "pipeline-stage", "depth": 2, "depends_on": list(depends),
                     "role": role, "write_scope": [node_id + "/**"], "resource_class": "normal",
                     "completion_gate": f"{capability.split('-')[-1]}-{node_id}"})
    record = {"schema_version": 1, "capability": capability, "capability_mode": mode,
              "requested_intensity": "standard", "effective_intensity": "standard",
              "execution_topology": "staged", "owner_depth": 1, "tracking": "tracked",
              "spec_touch": False, "display_title": title, "cwd": cwd, "artifact_root": cwd + "/.agent_reports",
              "nodes": rows, "completion_gates": [row["completion_gate"] for row in rows],
              "human_gates": []}
    digest = routemod.route_hash(record)
    record["route_hash"] = digest
    record["route_id"] = "rt-" + digest.split(":", 1)[1][:16]
    return record


def _done(evidence, route_id, node, route_file, minutes_ago, harness, model, effort, now):
    """Record one stage as finished in the node-evidence map ``attach_projections`` reads."""
    evidence.setdefault(route_id, {})[node] = {
        "status": "done", "slug": f"{node}-done", "ts": now - minutes_ago * 60, "pid": None,
        "harness": harness, "model": model, "effort": effort,
        "completion_gate": f"{node}-gate", "note": None, "route_file": route_file, "route_hash": None}


def snapshot(now=None, workdir=None):
    """``(LiveSnapshot, compute_hosts)`` for the README: four projects, three runtimes, two routes."""
    from tools.fleet.model import DispatchJob as J, Session as S
    from tools.fleet.projection import attach_projections
    from tools.fleet.refresh import LiveSnapshot, fleet_answer_wait_view
    now = NOW if now is None else now
    if not workdir:
        # The route files live as long as this process may draw the snapshot.
        workdir = tempfile.mkdtemp(prefix="fleet-readme-demo-")
        atexit.register(shutil.rmtree, workdir, True)
    api, ml = f"{HOME}/web-api", f"{HOME}/ml-train"

    code = _route("autopilot-code", "dev", api, "Rate limiting",
                  [("plan", (), "deep maker"), ("execute", ("plan",), "fast implementer"),
                   ("test", ("execute",), "fast reviewer"), ("report", ("test",), "fast writer")])
    lab = _route("autopilot-lab", "eval", ml, "Checkpoint eval",
                 [("setup", (), "deep maker"), ("eval-asr", ("setup",), "fast implementer"),
                  ("eval-sep", ("setup",), "fast implementer"),
                  ("report", ("eval-asr", "eval-sep"), "fast writer")])
    files = {}
    for name, record in (("code", code), ("lab", lab)):
        files[name] = os.path.join(workdir, f"{name}.route.json")
        with open(files[name], "w", encoding="utf-8") as handle:
            json.dump(record, handle)
    evidence = {}
    _done(evidence, code["route_id"], "plan", files["code"], 31, "claude", "Opus 5.5", "xhigh", now)
    _done(evidence, lab["route_id"], "setup", files["lab"], 24, "claude", "Opus 5.5", "high", now)

    sessions = [
        # --- web-api: a working owner with a stage worker, a reviewer, a session waiting on you
        S(harness="claude", pid=71001, cwd=api, session_id="readme-claude-api",
          title="Rate limiting", model="Opus 5.5", effort="xhigh",
          ctx_pct=46, elapsed_min=72, branch="feat/rate-limit", liveness="working",
          session_tag="4f", herdr_attached=True, steward=True, mtime=now - 40,
          summary="Running the limiter tests"),
        S(harness="codex", pid=71002, cwd=api, session_id="readme-codex-api",
          title="Pagination review", model="gpt-5.5", effort="high",
          ctx_pct=71, rl_5h=24, rl_7d=41, rl_rs=(now + 2 * 3600 + 10 * 60, now + 4 * 86400), elapsed_min=38, branch="main", liveness="working",
          session_tag="b2", herdr_attached=True, mtime=now - 20,
          summary="Re-running the API tests for [4f]"),
        S(harness="claude", pid=71003, cwd=api, session_id="readme-claude-notes",
          title="Release notes 2.4", model="Sonnet 5.5", effort="medium",
          ctx_pct=33, elapsed_min=19, branch="main", liveness="blocked",
          session_tag="9c", herdr_attached=True, mtime=now - 420,
          interaction_state={"kind": "decision", "source": "claude-transcript",
                             "waiting_since": now - 7 * 60},
          summary="Choose a layout"),
        # --- ml-train: a lab owner, and a second runtime tuning the data loader
        S(harness="claude", pid=71004, cwd=ml, session_id="readme-claude-ml",
          title="Checkpoint eval", model="Opus 5.5", effort="high",
          ctx_pct=38, elapsed_min=54, branch="exp/checkpoint-eval", liveness="working",
          session_tag="d1", herdr_attached=True, steward=True, mtime=now - 90,
          summary="Waiting on the ASR eval"),
        S(harness="opencode", pid=71005, cwd=ml, session_id="readme-oc-ml",
          title="Loader workers", model="glm-5.2", effort="high",
          ctx_pct=29, elapsed_min=26, branch="main", liveness="working",
          session_tag="e8", mtime=now - 120,
          summary="Profiling 4, 8, 16 workers"),
        # --- docs-site and mobile-app: quiet projects
        S(harness="opencode", pid=71006, cwd=f"{HOME}/docs-site", session_id="readme-oc-docs",
          title="Broken links", model="glm-5.2", effort="medium",
          ctx_pct=14, elapsed_min=44, branch="main", liveness="idle",
          session_tag="31", mtime=now - 1500,
          summary="Link check is clean"),
        S(harness="claude", pid=71007, cwd=f"{HOME}/docs-site", session_id="readme-claude-docs",
          title="Quick start", model="Sonnet 5.5", effort="medium",
          ctx_pct=63, elapsed_min=410, branch="main", liveness="idle",
          session_tag="7a", detached=True, mtime=now - 3 * 3600),
        S(harness="codex", pid=71008, cwd=f"{HOME}/mobile-app", session_id="readme-codex-mobile",
          title="Login crash", model="gpt-5.5", effort="medium",
          ctx_pct=21, rl_5h=24, rl_7d=41, rl_rs=(now + 2 * 3600 + 10 * 60, now + 4 * 86400), elapsed_min=185, branch="main",
          liveness="idle", session_tag="c5", mtime=now - 2 * 3600,
          summary="Fixed, test added"),
        # The execute worker of the web-api route is also a headless session (a stage worker).
        S(harness="claude", pid=72001, cwd=api + "-wt/execute", session_id="readme-worker-execute",
          slug="rate-limit-execute", model="Sonnet 5.5", effort="medium", is_child=True,
          elapsed_min=9, liveness="working", branch="feat/rate-limit-execute"),
    ]
    jobs = [
        J(key="code", slug="rate-limit", cwd=api, parent_sid="readme-claude-api", parent_cwd=api,
          liveness="working", depth=1, capability_owner="autopilot-code", harness="claude",
          model="Opus 5.5", effort="xhigh", elapsed_min=31, ctx_pct=46),
        J(key="code-execute", mode="dev", harness="claude", model="Sonnet 5.5", effort="medium",
          elapsed_min=9, slug="rate-limit-execute", cwd=api + "-wt/execute",
          parent_slug="rate-limit", is_child=True, depth=2, liveness="working", pid=72001,
          route_id=code["route_id"], route_file=files["code"], route_node="execute",
          ctx_pct=34),
        J(key="lab", slug="checkpoint-eval", cwd=ml, parent_sid="readme-claude-ml", parent_cwd=ml,
          liveness="working", depth=1, capability_owner="autopilot-lab", harness="claude",
          model="Opus 5.5", effort="high", elapsed_min=24, ctx_pct=38),
        J(key="lab-eval", mode="eval", harness="claude", model="Sonnet 5.5", effort="medium",
          elapsed_min=7, slug="checkpoint-eval-asr", cwd=ml + "-wt/eval-asr",
          parent_slug="checkpoint-eval", is_child=True, depth=2, liveness="working",
          route_id=lab["route_id"], route_file=files["lab"], route_node="eval-asr", ctx_pct=22),
    ]
    sessions, jobs = attach_projections(
        sessions, jobs, node_evidence=evidence, now=now, spec_markers={}, capability_groundings={})
    by_sid = {s.session_id: s for s in sessions}
    for session in sessions:
        session._prompt_display = {"human": None, "human_state": "none", "received": []}
    for index, (sid, text) in enumerate((
        ("readme-claude-api", "Keep the rate limit per API key"),
        ("readme-codex-api", "Add a test for the retry header"),
        ("readme-oc-ml", "Try four data-loader workers"),
    )):
        by_sid[sid]._prompt_display = {"human_state": "known", "human": {
            "sequence": 1, "at": now - (index + 1) * 60, "text": text},
            "received": [{"source": "peer", "sender": "Codex [b2]", "at": now - 30,
                          "text": "The regression test is ready"}]}
    api_session = by_sid["readme-claude-api"]
    codex = by_sid["readme-codex-api"]
    api_session.peer_last_sent = {"to_name": "web-api", "to_session_id": "readme-codex-api",
                                  "to_harness": "codex", "kind": "steer", "age_min": 26}
    codex.peer_last_recv = {"from_name": "web-api-4f", "from_session_id": "readme-claude-api",
                            "from_harness": "claude", "kind": "steer", "age_min": 26}
    by_sid["readme-claude-ml"].peer_last_sent = {
        "to_name": "ml-train", "to_session_id": "readme-oc-ml", "to_harness": "opencode",
        "kind": "steer", "age_min": 14}
    by_sid["readme-oc-ml"].peer_last_recv = {
        "from_name": "ml-train-d1", "from_session_id": "readme-claude-ml",
        "from_harness": "claude", "kind": "steer", "age_min": 14}
    api_session.work_projection = dataclasses.replace(
        api_session.work_projection,
        result={"name": "Fix the pagination off-by-one", "result": "success",
                "at": now - 52 * 60, "source": "route"})

    hosts = {"configured": True, "observed_at": now, "hosts": [
        {"host": "devbox", "self": True, "reachable": True, "cpu_utilization_pct": 31.0,
         "cpu_count": 32, "memory_used_mib": 51200, "memory_total_mib": 131072,
         "gpus": [{"index": 0, "name": "RTX 6000 Ada", "utilization_gpu_pct": 93,
                   "memory_used_mib": 31744, "memory_total_mib": 49152,
                   "processes": [{"pid": 73001, "process_name": "python", "cwd": ml,
                                  "command": "python eval_asr.py --ckpt best.pt",
                                  "used_memory_mib": 31232, "elapsed_s": 7 * 60,
                                  "session_owner": {"harness": "claude",
                                                    "session_id": "readme-claude-ml"}}]},
                  {"index": 1, "name": "RTX 6000 Ada", "utilization_gpu_pct": 0,
                   "memory_used_mib": 512, "memory_total_mib": 49152, "processes": []}]},
        {"host": "gpu-box", "self": False, "reachable": True, "cpu_utilization_pct": 12.0,
         "cpu_count": 64, "memory_used_mib": 20480, "memory_total_mib": 262144,
         "gpus": [{"index": 0, "name": "A100", "utilization_gpu_pct": 64, "memory_used_mib": 28672,
                   "memory_total_mib": 81920,
                   "processes": [{"pid": 73002, "process_name": "python", "cwd": ml,
                                  "command": "python bench_loader.py --workers 16",
                                  "used_memory_mib": 28160, "elapsed_s": 11 * 60,
                                  "session_owner": {"harness": "opencode",
                                                    "session_id": "readme-oc-ml"}}]},
                  {"index": 1, "name": "A100", "utilization_gpu_pct": 0, "memory_used_mib": 0,
                   "memory_total_mib": 81920, "processes": []}]}]}

    memory = {
        "journal_available": True, "graveyard_available": False,
        "today": {"added_working": 3, "added_durable": 2, "added": 5, "expired": 1, "pruned": 0},
        "last_distill_min": 95, "alerts": {"durable_over": [], "distill_stale": False},
        "by_repo": {"web-api": [
            {"ts": _iso(now - 41 * 60), "action": "decision-record", "type": "decision",
             "project": "web-api", "snippet": "Use a sliding-window limiter, not a token bucket"},
            {"ts": _iso(now - 2 * 3600), "action": "decision-record", "type": "decision",
             "project": "web-api", "snippet": "Keep the v1 pagination cursor format"},
            {"ts": _iso(now - 41 * 60), "action": "add", "type": "decision",
             "snippet": "use a sliding-window limiter, not a token bucket"},
            {"ts": _iso(now - 95 * 60), "action": "update", "type": "project-progress",
             "snippet": None}],
            "ml-train": [
            {"ts": _iso(now - 3 * 3600), "action": "decision-record", "type": "decision",
             "project": "ml-train", "snippet": "Evaluate on the 16 kHz subset first"}]},
        "recent": [{"ts": _iso(now - 41 * 60), "action": "add", "type": "decision",
                    "snippet": "use a sliding-window limiter, not a token bucket"}],
        "periodic_curate": None}

    items = [
        {"id": "q1", "source": "S1-decision", "kind": "decision",
         "since": now - 7 * 60, "stale": "current", "harness": "claude",
         "session_id": "readme-claude-notes"},
        {"id": "q0", "source": "S4", "kind": "approval",
         "since": now - 3 * 3600, "stale": "older", "harness": "codex",
         "session_id": "readme-codex-mobile"}]
    by_session = {"claude\0readme-claude-notes": ["q1"]}
    waits = fleet_answer_wait_view({
        "items": items, "by_session": by_session, "unattributed": [], "total": 1, "older": 1,
        "health": [], "observed_at": now, "complete": True})

    usage = {"claude": {"observed_at": now - 30, "freshness": "fresh", "payload": {
        "rl_5h": 38, "rl_7d": 52, "rs_5h": now + 3 * 3600 + 20 * 60, "rs_7d": now + 3 * 86400,
        "rl_ms": []}}}
    snapshot = LiveSnapshot(sessions=sessions, jobs=jobs, memory=memory, usage_snapshots=usage,
                        hearting={"version": VERSION, "install_method": "managed"},
                        answer_waits=waits,
                        observations={"optional:peers": {"state": "success", "last_good": False,
                                                         "observed_at": now}})
    # Both GPU processes belong to the sessions above; without this the Explorer would compute
    # "not registered" rows from the host list it was handed.
    snapshot.unregistered_gpu = ()
    return snapshot, hosts


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(epoch))


# The owner's raw output the README's "output" screen shows: tool calls and results in order.
RAW_OUTPUT = (
    "user: Compare the ASR and separation checkpoints on the 16 kHz subset.",
    "assistant: I'll read the eval config, then run setup before splitting the two evaluations.",
    "tool Read: configs/eval.yaml",
    "tool output: 42 lines",
    "assistant: Setup stage approved. Preparing the 16 kHz subset first.",
    "tool Edit: configs/eval.yaml",
    "tool output: set sample_rate: 16000",
    "tool Bash: python scripts/prepare_subset.py --rate 16000",
    "description: Prepare the 16 kHz evaluation subset",
    "tool output: wrote 1,200 clips to data/subset-16k",
    "assistant: Subset ready. eval-asr and eval-sep run as separate workers; waiting on eval-asr.",
)


def frame(render, snapshot, state, cols, rows, raw_for_route=None):
    """One Explorer frame: ``render``'s compose path, redrawn with a fixed blink phase.

    ``raw_for_route`` is ``(route_id, attempt_id)``: the owner's raw output (``RAW_OUTPUT``) is
    attached to that route's page, the way a live selection read attaches the recorded one.
    """
    from tools.fleet.explorer import frame as explorer_frame
    from tools.fleet.explorer.view_model import freeze
    render._explorer_compose(snapshot, cols, rows, state, now=NOW)
    model = render._EXPLORER_MODEL
    if raw_for_route is not None:
        route_id, attempt = raw_for_route
        model = dataclasses.replace(model, attempts=freeze(
            {"att": attempt, "read": "owner output", "lines": list(RAW_OUTPUT), "route": route_id}))
    return explorer_frame.draw(model, state, cols, rows, beat=1)


def route_id(snapshot, capability):
    """The id of the demo route a capability owns (``code`` or ``lab``)."""
    for job in snapshot.jobs:
        if job.key == capability and getattr(job, "route_id", None):
            return job.route_id
    for job in snapshot.jobs:
        if getattr(job, "route_node", None) and capability in job.key:
            return job.route_id
    return None
