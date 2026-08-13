"""Synthetic fixture for rendering checks (`fleet.py --demo` or FLEET_DEMO=1).

Covers all three harnesses, every liveness state, git branch, nested
dispatch under a parent, depth-2 subdispatch, an orphan, a loop, varied models/effort — so the render layer can be
exercised without waiting for real live processes. Merged INTO live data by fleet.py's --demo
path. branch is set explicitly here (fake cwds are not real repos).

F-30 (v10): also seeds 3 route-carrying jobs (a resolved record, a fan-out/fan-in parallel
record, and a record-less "degrade" job) + 1 subagent-bearing session — without these, `--view
process`/`p` renders "no active route" against demo data and the V3 design critic capture would
be reviewing a blank screen (plan §5, checklist Y4). The route fixtures referenced here are the
SAME files `tests/fixtures/route/` ships (this module is a mirrored fixture module, so the
relative path resolves identically in both `tools/fleet/` and the adapter mirror).
"""
import os
import time

from .model import Session, DispatchJob, SubAgent

_ROUTE_FIXDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "tests", "fixtures", "route")
_DEMO_CARD_ROUTE = os.path.join(_ROUTE_FIXDIR, "demo_card.json")
_DEMO_CARD_RID = "rt-271492e352d13180"   # fabricated content -> hash cannot collide with a real record
_LAB_ROUTE = os.path.join(_ROUTE_FIXDIR, "synth_parallel_lab.json")
_LAB_RID = "rt-6f5423d05eaf3189"
_COMPOSED_ROUTE = os.path.join(_ROUTE_FIXDIR, "synth_composed_survey.json")
_COMPOSED_RID = "rt-63788ad671654b75"


def _seed_route_evidence():
    """Best-effort: stamps synthetic done evidence so the process view has at least one ✓ node
    to show (§5.3's "n/m nodes" progress) without a real jobs.log row backing it. Merges (never
    overwrites) — a live `dispatch.collect()` call on the same tick keeps its own real evidence
    for real route_ids.

    ★ design_review_round_1.md priority-🟡1: the parallel-lab card's `setup` node MUST be seeded
    too — its fan-out children (eval-asr/eval-sep below) are `active`/`failed`, and a node
    cannot logically be `pending` (unstarted) while its OWN dependents are already running or
    failed. Leaving `setup` unseeded drew an impossible DAG (`setup ○` with live/failed
    children) on F-30's own marquee demo screen — fixed by seeding `setup` done here, exactly
    like `plan` is seeded for the resolved-record card."""
    try:
        from .collectors import dispatch
        existing = dict(getattr(dispatch.collect, "last_route_nodes", None) or {})
        existing.setdefault(_DEMO_CARD_RID, {}).setdefault("plan", {
            "status": "done", "slug": "demo-route-conductor-plan", "ts": time.time() - 900,
            "pid": None, "harness": "claude", "model": "opus", "effort": "high",
            "completion_gate": "code-plan", "note": None,
            "route_file": _DEMO_CARD_ROUTE, "route_hash": None,
        })
        existing.setdefault(_LAB_RID, {}).setdefault("setup", {
            "status": "done", "slug": "demo-lab-conductor-setup", "ts": time.time() - 600,
            "pid": None, "harness": "claude", "model": "opus", "effort": "high",
            "completion_gate": "lab-setup", "note": None,
            "route_file": _LAB_ROUTE, "route_hash": None,
        })
        existing.setdefault(_COMPOSED_RID, {}).setdefault("survey", {
            "status": "done", "slug": "demo-composed-survey", "ts": time.time() - 480,
            "pid": None, "harness": "claude", "model": "opus", "effort": "high",
            "completion_gate": "research-retrieval", "note": None,
            "route_file": _COMPOSED_ROUTE, "route_hash": None,
        })
        dispatch.collect.last_route_nodes = existing
    except Exception:
        pass


def collect(harness_filter=None):
    _seed_route_evidence()
    S, J = Session, DispatchJob
    sessions = [
        S(harness="codex", pid=89990, cwd="/home/demo/demo-app",
          session_id="demo-periodic-curator", slug="periodic-curator",
          title="memory periodic curator", elapsed_min=11, liveness="idle",
          mem_worker=True),
        # --- project 'demo-app' ---
        S(harness="claude", pid=90001, cwd="/home/demo/demo-app", session_id="demo-claude-1",
          slug="demo-app-a7", model="Opus 4.8 (1M context)", effort="xhigh",
          ctx_pct=45, rl_5h=33, rl_7d=12, rl_ms=[["fable", 57]], cost=12.30, elapsed_min=95,
          status="busy", branch="main", liveness="working",
          # F-16/F-17 merge demo — the live subtitle row under a session row.
          summary="지금 render.py 그룹 루프의 틴트 적용 경로를 분석 중"),
        # Deterministic composed-DAG owner: the group row must show both active
        # claim siblings and the route's 1/4 progress with providers disabled.
        S(harness="claude", pid=90008, cwd="/home/demo/demo-app", session_id="demo-composed-owner",
          slug="demo-composed-owner", model="Opus 4.8", effort="high", ctx_pct=54,
          elapsed_min=18, branch="feat/composed", liveness="working"),
        S(harness="codex", pid=90002, cwd="/home/demo/demo-app", session_id="demo-codex-1",
          slug="demo-app", model="gpt-5.5", effort="high",
          ctx_pct=72, rl_5h=94, rl_7d=53, elapsed_min=41,
          branch="feat/streaming", liveness="idle"),
        # F-60: blocked sessions — the whole point of the red key + reverse chip is that these
        # rows are findable at a glance, so the demo board has to contain some. Two producer
        # kinds exercise Fleet's shared `approval` label, and one sits in the same group as a
        # stale row so the red-vs-red comparison is on screen.
        S(harness="claude", pid=90011, cwd="/home/demo/demo-app", session_id="demo-blocked-1",
          slug="demo-app-gate", model="Opus 5", effort="xhigh",
          ctx_pct=37, rl_5h=33, rl_7d=12, cost=2.80, elapsed_min=7,
          branch="feat/gate", liveness="blocked",
          interaction_state={"kind": "approval", "source": "claude-transcript",
                             "waiting_since": time.time() - 7 * 60},
          summary="배포 승인 대기 중 — 사용자의 확인을 기다리고 있습니다"),
        # --- project 'demo-lib' ---
        S(harness="opencode", pid=90003, cwd="/home/demo/demo-lib", session_id="demo-oc-1",
          slug="witty-orchid", model="deepseek-v4-pro", effort="high",
          ctx_pct=8, cost=0.05, elapsed_min=16,
          branch="wip/experiment", liveness="idle"),
        S(harness="claude", pid=90004, cwd="/home/demo/demo-lib", session_id="demo-claude-2",
          slug="demo-lib-f0", model="Sonnet 5", effort="medium",
          ctx_pct=88, rl_5h=20, rl_7d=40, cost=3.10, elapsed_min=3000,
          branch="fix/bug-4821", liveness="stale"),
        # --- project 'demo-svc' (opencode, low effort) ---
        S(harness="opencode", pid=90005, cwd="/home/demo/demo-svc", session_id="demo-oc-2",
          slug="brave-comet", model="glm-5.2", effort="low",
          ctx_pct=31, cost=0.42, elapsed_min=200,
          branch="main", liveness="working"),
        S(harness="codex", pid=90012, cwd="/home/demo/demo-svc", session_id="demo-blocked-2",
          slug="demo-svc-perm", model="gpt-5.6-sol", effort="xhigh",
          ctx_pct=64, rl_5h=94, rl_7d=53, elapsed_min=22,
          branch="fix/perm", liveness="blocked",
          interaction_state={"kind": "permission", "source": "codex-rollout",
                             "waiting_since": time.time() - 22 * 60},
          summary="쉘 명령 권한 승인을 기다리는 중"),
        # F-47 v47: a session parked on a wait primitive — idle glyph + dim ⏳ badge; the
        # sleep child never promotes the row to working.
        S(harness="claude", pid=90013, cwd="/home/demo/demo-lib", session_id="demo-wait-1",
          slug="demo-lib-wait", model="Opus 5", effort="high",
          ctx_pct=22, elapsed_min=34, branch="chore/poll", liveness="idle",
          status="shell", exec_child={"pid": 90113, "comm": "sleep", "etime_s": 240},
          summary="CI 결과 폴링 대기 — 다음 검사까지 sleep"),
        # detached tmux session (no client attached) — idle but backgrounded, shown with ◌ not ○
        S(harness="claude", pid=90006, cwd="/home/demo/demo-app", session_id="demo-claude-3",
          slug="demo-app-detach", model="Opus 4.8", effort="high", detached=True,
          ctx_pct=62, rl_5h=40, rl_7d=22, cost=8.40, elapsed_min=720,
          branch="fix/night-run", liveness="idle"),
        # --- project 'demo-cool' (cooling) — no active work, last transcript write ~92min ago; the
        # idle session lingers (within the 48h live window) so the group isn't folded, and the
        # The directory header shows a grey ring and time since completion while cooling.
        S(harness="claude", pid=90007, cwd="/home/demo/demo-cool", session_id="demo-claude-cool",
          slug="demo-cool-shipped", model="Opus 4.8", effort="high",
          ctx_pct=18, rl_5h=25, rl_7d=15, cost=4.20, elapsed_min=160,
          branch="feat/shipped", liveness="idle",
          mtime=time.time() - 92 * 60),
        # --- F-30 (v10): the depth-2 "execute" stage worker of the resolved-record route card
        # below IS this session (same pid, is_child — headless `claude -p` stage workers show
        # as BOTH a job and a session, §5.3.1). Its subagents prove the pid-join independent of
        # the group view's own is_child filtering (this session is invisible there by design).
        S(harness="claude", pid=95001, cwd="/home/demo/route-app-wt/execute",
          session_id="demo-route-execute", slug="demo-route-conductor-execute",
          model="Opus 4.8", effort="high", is_child=True, elapsed_min=8, liveness="working",
          branch="v10-execute",
          subagents=[SubAgent(agent_type="explore", active=True, started_at=time.time() - 120,
                              source="claude-sidechain",
                              model="claude-sonnet-5", effort="medium")]),
    ]
    jobs = [
        # nested under the demo-app claude parent (demo-claude-1)
        J(key="code", stage="exec", mode="dev", qa="adversarial", qa_source="argv", harness="claude",
          model="Opus 4.8 (1M context)", elapsed_min=22, slug="demo-feat-x",
          cwd="/home/demo/demo-app-wt/feat-x", parent_sid="demo-claude-1", parent_cwd="/home/demo/demo-app", is_child=True,
          branch="feat-x", liveness="working", depth=1, intensity="adversarial",
          worker_role="capability-owner", capability_owner="autopilot-code",
          ctx_pct=38, active_context_tokens=380000, context_window_tokens=1000000,
          exec_tool={"name": "pytest"},
          # F-16/F-17 merge demo — a dispatch row's own adopted live subtitle.
          summary="지금 feat-x 브랜치의 테스트 실패를 재현하는 중"),
        # depth-2 workers owned by the autopilot-code capability worker above
        J(key="plan", stage="done", mode="review", qa="adversarial", qa_source="jobslog", harness="codex",
          model="gpt-5.5", elapsed_min=6, slug="demo-feat-x-plan-alt",
          cwd="/home/demo/demo-app-wt/feat-x-plan-alt", parent_slug="demo-feat-x", is_child=True,
          branch="feat-x-plan-alt", liveness="idle", depth=2, intensity="adversarial",
          ctx_pct=47, active_context_tokens=100000, context_window_tokens=200000,
          worker_role="planner", capability_owner="autopilot-code"),
        J(key="test", stage="test", mode="verify", qa="adversarial", qa_source="jobslog", harness="opencode",
          model="glm-5.2", elapsed_min=4, slug="demo-feat-x-verifier",
          cwd="/home/demo/demo-app-wt/feat-x-verifier", parent_slug="demo-feat-x", is_child=True,
          branch="feat-x-verifier", liveness="working", depth=2, intensity="adversarial",
          worker_role="verifier", capability_owner="autopilot-code"),
        J(key="review", stage="test", mode="debug", qa="quick", qa_source="jobslog", harness="codex",
          model="gpt-5.5", elapsed_min=8, slug="demo-review",
          cwd="/home/demo/demo-app-wt/review", parent_sid="demo-claude-1", parent_cwd="/home/demo/demo-app", is_child=True,
          branch="review", liveness="working", depth=1),
        # drill launched by a Codex parent from a throwaway /tmp fixture, with depth-2
        # cross-harness checks. It should render under the parent, not as a /tmp project.
        J(key="drill", stage="run", mode="loop/drill", qa="quick", qa_source="jobslog", harness="codex",
          model="gpt-5.5", elapsed_min=3, slug="drill-g6-worktree",
          cwd="/tmp/drill-g6_worktree_dispatch-abcd/repo", parent_sid="demo-codex-1",
          parent_cwd="/home/demo/demo-app", is_child=True, branch="main", liveness="working",
          depth=1, intensity="quick", worker_role="g6_worktree_dispatch", capability_owner="drill"),
        J(key="drill", stage="review", mode="loop/drill-verify", qa="quick", qa_source="jobslog", harness="claude",
          model="Sonnet 5", elapsed_min=2, slug="drill-g6-claude-verify",
          cwd="/tmp/drill-g6_worktree_dispatch-verify/repo", parent_slug="drill-g6-worktree",
          parent_cwd="/home/demo/demo-app", is_child=True, liveness="idle", depth=2,
          intensity="quick", worker_role="verifier", capability_owner="drill"),
        J(key="drill", stage="review", mode="loop/drill-verify", qa="quick", qa_source="jobslog", harness="opencode",
          model="glm-5.2", elapsed_min=1, slug="drill-g6-opencode-verify",
          cwd="/tmp/drill-g6_worktree_dispatch-verify2/repo", parent_slug="drill-g6-worktree",
          parent_cwd="/home/demo/demo-app", is_child=True, liveness="working", depth=2,
          intensity="quick", worker_role="verifier", capability_owner="drill"),
        # nested under demo-svc opencode parent
        J(key="spec", stage="design", mode="dev", qa="thorough", qa_source="plan", harness="opencode",
          model="glm-5.2", elapsed_min=5, slug="demo-spec",
          cwd="/home/demo/demo-svc-wt/spec", parent_sid="demo-oc-2", parent_cwd="/home/demo/demo-svc", is_child=True,
          branch="spec", liveness="working"),
        # orphan (parent not on screen) — stale, in demo-lib
        J(key="debug", stage="running", mode="debug", qa="thorough", qa_source="jobslog",
          harness="codex", elapsed_min=290 * 60, slug="demo-orphan",
          cwd="/home/demo/demo-lib-wt/orphan", parent_sid="demo-dead", parent_cwd="/home/demo/demo-lib", is_child=True,
          branch="orphan", liveness="stale"),
        # loop
        J(key="oncall", elapsed_min=12, slug="oncall", cwd="", parent_sid=None, liveness="working"),
        # --- F-30 (v10) route cards: 1 resolved record (plan✓ via _seed_route_evidence, execute
        # active — pid=95001 joins the session above), 1 fan-out/fan-in parallel record with a
        # FAILED branch (eval-sep), 1 record-less "degrade" pipeline (§5.3's honest-gap card).
        J(key="code", slug="demo-route-conductor", cwd="/home/demo/route-app",
          parent_sid="demo-claude-1", liveness="working", depth=1,
          capability_owner="autopilot-code"),
        J(key="code-execute", mode="dev", harness="claude", model="Opus 4.8", effort="high",
          elapsed_min=8, slug="demo-route-conductor-execute",
          cwd="/home/demo/route-app-wt/execute", parent_slug="demo-route-conductor",
          is_child=True, depth=2, liveness="working", pid=95001,
          route_id=_DEMO_CARD_RID, route_file=_DEMO_CARD_ROUTE, route_node="execute"),
        J(key="claim", slug="demo-composed-claim-b", cwd="/home/demo/demo-app-wt/claim-b",
          parent_sid="demo-composed-owner", parent_cwd="/home/demo/demo-app",
          harness="claude", model="Sonnet 5", effort="medium", elapsed_min=6,
          liveness="working", depth=2, is_child=True, assigned_contract="autopilot-code",
          route_id=_COMPOSED_RID, route_file=_COMPOSED_ROUTE, route_node="claim-b"),
        J(key="claim", slug="demo-composed-claim-a", cwd="/home/demo/demo-app-wt/claim-a",
          parent_sid="demo-composed-owner", parent_cwd="/home/demo/demo-app",
          harness="claude", model="Sonnet 5", effort="medium", elapsed_min=6,
          liveness="working", depth=2, is_child=True, assigned_contract="autopilot-code",
          route_id=_COMPOSED_RID, route_file=_COMPOSED_ROUTE, route_node="claim-a"),
        J(key="lab", slug="demo-lab-conductor", cwd="/home/demo/lab-project",
          liveness="working", depth=1, capability_owner="autopilot-lab"),
        J(key="lab-eval", mode="eval", harness="claude", model="Sonnet 5", effort="medium",
          elapsed_min=9, slug="demo-lab-conductor-eval-asr",
          cwd="/home/demo/lab-project-wt/eval-asr", parent_slug="demo-lab-conductor",
          is_child=True, depth=2, liveness="working",
          route_id=_LAB_RID, route_file=_LAB_ROUTE, route_node="eval-asr"),
        J(key="lab-eval", mode="eval", harness="claude", model="Sonnet 5", effort="medium",
          elapsed_min=3, slug="demo-lab-conductor-eval-sep",
          cwd="/home/demo/lab-project-wt/eval-sep", parent_slug="demo-lab-conductor",
          is_child=True, depth=2, liveness="stale",
          route_id=_LAB_RID, route_file=_LAB_ROUTE, route_node="eval-sep"),
        # record-less "degrade" pipeline — no route_id at all (§5.3 "no route record" card).
        J(key="spec", stage="design", mode="dev", qa="standard", qa_source="argv",
          harness="opencode", model="glm-5.2", elapsed_min=14, slug="demo-degrade-spec",
          cwd="/home/demo/degrade-project", liveness="working", depth=1),
    ]
    if harness_filter:
        sessions = [s for s in sessions if s.harness in harness_filter]
    return sessions, jobs


def memory_snapshot():
    """Demo-only active periodic batch; never touches the real memory store."""
    return {
        "journal_available": False, "graveyard_available": False,
        "today": {"added_working": 0, "added_durable": 0, "added": 0,
                  "expired": 0, "pruned": 0},
        "last_distill_min": None, "recent": [], "by_repo": {},
        "alerts": {"durable_over": [], "distill_stale": False},
        "periodic_curate": {"done": 1, "total": 3,
                             "current_cwd": "/home/demo/demo-lib", "elapsed_s": 687},
    }
