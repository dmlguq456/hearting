#!/usr/bin/env python3

from __future__ import annotations

import json
import importlib.util
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "hooks" / "material-route-guard.py"
ROUTER = ROOT / "utilities" / "capability-route.py"
PREDICATES = (
    "atomic-outcome",
    "known-scope",
    "no-shared-contract",
    "no-resource-run",
    "no-artifact-handoff",
    "no-independent-verifier",
    "focused-verification",
)
SPEC = importlib.util.spec_from_file_location("material_route_guard", GUARD)
assert SPEC and SPEC.loader
MATERIAL_GUARD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MATERIAL_GUARD
SPEC.loader.exec_module(MATERIAL_GUARD)


class MaterialRouteGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self._original_agent_home = os.environ.get("AGENT_HOME")
        os.environ["AGENT_HOME"] = str(ROOT)
        self.addCleanup(self._restore_agent_home)
        self.base = Path(self.temp.name)
        self.repo = self.base / "project"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test"], check=True)
        (self.repo / "app.py").write_text("print('one')\n", encoding="utf-8")
        (self.repo / "README.md").write_text("one\n", encoding="utf-8")
        (self.repo / "settings.json").write_text("{}\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "initial"], check=True)
        self.artifacts = self.base / "artifacts"
        self.artifacts.mkdir()
        self.home = self.base / "agent-home"
        (self.home / "core").mkdir(parents=True)
        (self.home / "core" / "CORE.md").write_text("core\n", encoding="utf-8")
        (self.home / "utilities").symlink_to(ROOT / "utilities", target_is_directory=True)
        command = [
            sys.executable, str(ROUTER), "compile",
            "--slug", "material-route-fixture",
            "--capability", "autopilot-code",
            "--capability-mode", "dev",
            "--intensity", "direct",
            "--cwd", str(self.repo),
            "--artifact-root", str(self.artifacts),
        ]
        for predicate in PREDICATES:
            command += ["--predicate", predicate]
        command += [
            "--transport", "interactive",
            "--inline-reason", "atomic-direct",
            "--tracking", "untracked",
            "--spec-read", "not-applicable",
            "--drift-verdict", "no-project-spec",
            "--workflow-mode", "untracked",
            "--artifact-guard", "preflight-passed",
        ]
        compile_env = os.environ.copy()
        compile_env["AGENT_HOME"] = str(ROOT)
        compile_env.pop("AGENT_DISPATCH_JOBS", None)
        result = subprocess.run(command, text=True, capture_output=True, env=compile_env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.route_id = json.loads(result.stdout)["route_id"]
        self.route = self.artifacts / ".runtime" / "routes" / f"{self.route_id}.json"
        self.assertTrue(self.route.is_file())
        self.receipts = self.base / "recall-opportunities"

    def _restore_agent_home(self) -> None:
        if self._original_agent_home is None:
            os.environ.pop("AGENT_HOME", None)
        else:
            os.environ["AGENT_HOME"] = self._original_agent_home

    def opportunity(
        self, session: str = "session-a", *, turn: str = "", cwd: Path | None = None,
        source: str = "candidate-probe", created_at_ns: int | None = None,
        result_ids: list[str] | None = None,
    ) -> Path:
        self.receipts.mkdir(parents=True, exist_ok=True)
        path = self.receipts / f"{MATERIAL_GUARD.recall_session_key(session)}.json"
        value = {
            "schema_version": 1,
            "session_digest": MATERIAL_GUARD.recall_session_key(session),
            "turn_digest": MATERIAL_GUARD.recall_turn_digest(turn),
            "project": "test-project",
            "cwd": str((cwd or self.repo).resolve()),
            "source": source,
            "result_count": len(result_ids or []),
            "result_ids": list(result_ids or []),
            "created_at_ns": created_at_ns if created_at_ns is not None else time.time_ns(),
        }
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def guard(
        self, *args: str, session: str = "session-a",
        env: dict[str, str] | None = None, opportunity: bool = True,
    ):
        turn = ""
        if "--turn" in args:
            index = args.index("--turn")
            if index + 1 < len(args):
                turn = args[index + 1]
        if opportunity:
            self.opportunity(session, turn=turn)
        clean = {key: value for key, value in os.environ.items()
                 if key not in {"AGENT_ROUTE_FILE", "AGENT_ROUTE_ID", "AGENT_ROUTE_NODE"}}
        return subprocess.run(
            [
                sys.executable, str(GUARD), "--agent-home", str(self.home),
                "check", *args, "--cwd", str(self.repo), "--session", session,
            ],
            text=True,
            capture_output=True,
            env={**clean, "MEM_RECALL_RECEIPTS": str(self.receipts), **(env or {})},
        )

    def close_route(self) -> Path:
        """Write the closure sidecar the way `capability-route.py close` does."""
        sidecar = self.route.with_name(self.route.stem + ".outcome.json")
        sidecar.write_text(
            json.dumps({
                "schema_version": 3,
                "route_id": self.route_id,
                "route_file": str(self.route),
                "closed_at": "2026-09-04T00:00:00Z",
                "terminal_gate_proven": True,
            }),
            encoding="utf-8",
        )
        return sidecar

    def bind(self, session: str = "session-a") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable, str(GUARD), "--agent-home", str(self.home),
                "bind", "--route", str(self.route), "--cwd", str(self.repo),
                "--session", session,
            ],
            text=True,
            capture_output=True,
        )

    def reset(self) -> None:
        subprocess.run(["git", "-C", str(self.repo), "reset", "--hard", "-q", "HEAD"], check=True)

    def test_source_edit_denies_silent_no_route_and_accepts_bound_route(self) -> None:
        denied = self.guard("--tool", "Edit", "--file", str(self.repo / "app.py"))
        self.assertEqual(denied.returncode, 2)
        self.assertIn("silent no-route", denied.stderr)
        self.assertEqual(self.bind().returncode, 0)
        allowed = self.guard("--tool", "Edit", "--file", str(self.repo / "app.py"))
        self.assertEqual(allowed.returncode, 0, allowed.stderr)

    def test_bind_and_artifact_guard_check_agree_without_agent_home_override(self) -> None:
        """I-6 regression (assignment 검증요구 (b) / plan-check round-1 T2):
        without an explicit --agent-home override, a direct `bind` and a
        `check` reached through hooks/artifact-guard.sh (which used to inject
        its own --agent-home, unconditionally overriding whatever env the
        caller set) must resolve the SAME `.route-grounding` state directory
        purely from AGENT_HOME env -- otherwise a bound route is invisible to
        the artifact-guard-mediated check, or vice versa."""
        self.opportunity("session-env-only")
        clean = {
            key: value for key, value in os.environ.items()
            if key not in {"AGENT_ROUTE_FILE", "AGENT_ROUTE_ID", "AGENT_ROUTE_NODE", "AGENT_HOME"}
        }
        env = {**clean, "AGENT_HOME": str(self.home), "MEM_RECALL_RECEIPTS": str(self.receipts)}

        bind = subprocess.run(
            [sys.executable, str(GUARD), "bind", "--route", str(self.route),
             "--cwd", str(self.repo), "--session", "session-env-only"],
            text=True, capture_output=True, env=env,
        )
        self.assertEqual(bind.returncode, 0, bind.stderr)

        marker_path = MATERIAL_GUARD.marker_path(self.home, "session-env-only")
        self.assertTrue(marker_path.is_file(), "bind must write under AGENT_HOME/.route-grounding")

        artifact_root = self.base / "guard-artifacts" / ".agent_reports"
        artifact_root.mkdir(parents=True)
        target = artifact_root / "notes.md"
        route_record = json.loads(self.route.read_text())
        check = subprocess.run(
            ["bash", str(ROOT / "hooks" / "artifact-guard.sh"),
             "--file", str(target), "--session", "session-env-only"],
            text=True, capture_output=True,
            env={
                **env,
                "AGENT_ARTIFACT_ROOT": str(artifact_root),
                "AGENT_ROUTE_FILE": str(self.route),
                "AGENT_ROUTE_ID": route_record["route_id"],
                "AGENT_ROUTE_NODE": "",
            },
            cwd=str(self.repo),
        )
        self.assertEqual(check.returncode, 0, check.stderr)

        # The check path must have read the SAME marker bind wrote -- not a
        # second, disagreeing root -- proving both surfaces resolved AGENT_HOME
        # identically with no override on either call.
        direct_check = subprocess.run(
            [sys.executable, str(GUARD), "check", "--tool", "ArtifactWrite",
             "--file", str(target), "--cwd", str(self.repo), "--session", "session-env-only"],
            text=True, capture_output=True, env=env,
        )
        self.assertEqual(direct_check.returncode, 0, direct_check.stderr)
        self.assertEqual(
            MATERIAL_GUARD.marker_path(self.home, "session-env-only"), marker_path,
        )

    def test_bind_and_artifact_guard_check_agree_with_no_agent_home_at_all(self) -> None:
        """Review F-4: the case above sets AGENT_HOME, so both chains read the
        same first candidate and any implementation passes. The defect this
        cycle fixed (artifact-guard.sh pinning --agent-home to its own script
        location) only fires with AGENT_HOME/CLAUDE_HOME unset, so exercise
        that exact cell: an isolated $HOME whose only marked candidate is
        $XDG_DATA_HOME/hearting/current — the shared managed-release default of
        utilities/agent-home.sh —
        must be where BOTH the direct bind and the artifact-guard-mediated
        check keep their `.route-grounding` state."""
        self.opportunity("session-no-home")
        fake_home = self.base / "fakehome"
        fallback_home = fake_home / ".local" / "share" / "hearting" / "current"
        (fallback_home / "core").mkdir(parents=True)
        (fallback_home / "core" / "CORE.md").write_text("core\n", encoding="utf-8")
        (fallback_home / "utilities").symlink_to(
            ROOT / "utilities", target_is_directory=True
        )
        clean = {
            key: value for key, value in os.environ.items()
            if key not in {
                "AGENT_ROUTE_FILE", "AGENT_ROUTE_ID", "AGENT_ROUTE_NODE",
                "AGENT_HOME", "CLAUDE_HOME", "HOME", "XDG_DATA_HOME",
            }
        }
        env = {
            **clean,
            "HOME": str(fake_home),
            "XDG_DATA_HOME": str(fake_home / ".local" / "share"),
            "MEM_RECALL_RECEIPTS": str(self.receipts),
        }

        bind = subprocess.run(
            [sys.executable, str(GUARD), "bind", "--route", str(self.route),
             "--cwd", str(self.repo), "--session", "session-no-home"],
            text=True, capture_output=True, env=env,
        )
        self.assertEqual(bind.returncode, 0, bind.stderr)
        marker_path = MATERIAL_GUARD.marker_path(fallback_home, "session-no-home")
        self.assertTrue(
            marker_path.is_file(),
            "bind without AGENT_HOME must land in the shared managed-release default",
        )

        # The target must (a) sit inside self.route's own artifact_root, so
        # the route-artifact-root check doesn't short-circuit first, and (b)
        # fall under a recognized capability bucket ("plans"), since a bare
        # `.agent_reports/notes.md` is not classified as material at all and
        # `check_action` returns before ever resolving a route (review N-2's
        # underlying gap: the original target defeated the branch it meant to
        # pin regardless of the AGENT_ROUTE_* env vars below).
        artifact_root = self.artifacts / ".agent_reports"
        target = artifact_root / "plans" / "cycle-no-home" / "notes.md"
        target.parent.mkdir(parents=True)
        env_check = {**env, "AGENT_ARTIFACT_ROOT": str(artifact_root)}

        # No AGENT_ROUTE_FILE/AGENT_ROUTE_ID/AGENT_ROUTE_NODE here (review
        # N-2): supplying them makes artifact-guard's `is_worker` branch
        # true, which skips `_load_session_marker()` entirely and lets this
        # assertion pass even with the marker deleted. Omitting them forces
        # the session-marker branch this test is meant to pin.
        check = subprocess.run(
            ["bash", str(ROOT / "hooks" / "artifact-guard.sh"),
             "--file", str(target), "--session", "session-no-home"],
            text=True, capture_output=True,
            env=env_check,
            cwd=str(self.repo),
        )
        self.assertEqual(
            check.returncode, 0,
            f"artifact-guard check must read the marker bind wrote at "
            f"{marker_path}: {check.stderr}",
        )

        # Counter-proof: with the marker gone, the session-marker branch must
        # fail closed instead of silently agreeing (this is what the original
        # assertion failed to pin -- it passed with the marker deleted too).
        marker_path.unlink()
        check_without_marker = subprocess.run(
            ["bash", str(ROOT / "hooks" / "artifact-guard.sh"),
             "--file", str(target), "--session", "session-no-home"],
            text=True, capture_output=True,
            env=env_check,
            cwd=str(self.repo),
        )
        self.assertNotEqual(
            check_without_marker.returncode, 0,
            "artifact-guard check must fail closed once the session marker "
            "it is supposed to read is gone",
        )

    def test_non_git_research_exact_worktree_failure_cannot_write_inline_artifacts(self) -> None:
        project = self.base / "samsung-shaped"
        project.mkdir()
        artifacts = project / ".agent_reports"
        target = artifacts / "research" / "seminar" / "pipeline_state.yaml"
        route = artifacts / ".runtime" / "routes" / "research.json"
        evidence = self.base / "research-evidence.json"
        evidence.write_text(
            json.dumps({
                "tuples": [{
                    "parent_harness": "codex",
                    "parent_transport": "headless",
                    "parent_sandbox": "workspace-write",
                    "child_harness": "codex",
                    "launch_authority": "conductor",
                    "status": "unsupported",
                    "probe_source": "fixture-headless-check",
                    "probe_time": "2026-08-10T00:00:00Z",
                    "failure_class": "not-a-git-worktree",
                    "checked_worktree": str(project.resolve()),
                    "failure_scope": "exact-worktree",
                    "codex_command": "ok",
                    "retry_on_isolated_worktree": 1,
                }],
                "native_subagent": [{
                    "harness": "codex",
                    "transport": "headless",
                    "status": "unsupported",
                    "execution_surface": "codex-native-subagent",
                    "registered_worker": False,
                    "check_source": "user-policy:user-disabled",
                }],
            }),
            encoding="utf-8",
        )
        blocked_compile = subprocess.run(
            [
                sys.executable, str(ROUTER), "compile",
                "--slug", "blocked-research-fixture",
                "--capability", "autopilot-research",
                "--capability-mode", "academic",
                "--intensity", "standard",
                "--cwd", str(project),
                "--artifact-root", str(artifacts),
                "--signal", "source-fanout",
                "--transport", "headless",
                "--tracking", "untracked",
                "--spec-read", "not-applicable",
                "--drift-verdict", "no-project-spec",
                "--workflow-mode", "untracked",
                "--artifact-guard", "preflight-passed",
                "--dispatch-evidence", str(evidence),
                "--output", str(route),
            ],
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(blocked_compile.returncode, 0)
        self.assertIn(
            "dispatch-evidence-exact-worktree-reprobe-required",
            blocked_compile.stderr,
        )
        self.assertFalse(route.exists())

        denied = subprocess.run(
            [
                sys.executable, str(GUARD), "--agent-home", str(self.home),
                "check", "--tool", "ArtifactWrite", "--file", str(target),
                "--cwd", str(project), "--session", "research-session",
            ],
            text=True,
            capture_output=True,
            env={**os.environ, "MEM_RECALL_RECEIPTS": str(self.receipts)},
        )
        self.assertEqual(denied.returncode, 2)
        self.assertIn("session-route-missing", denied.stderr)
        self.assertFalse(target.exists())

        direct = [
            sys.executable, str(ROUTER), "compile",
            "--slug", "direct-research-fixture",
            "--capability", "autopilot-research",
            "--capability-mode", "academic",
            "--intensity", "direct",
            "--cwd", str(project),
            "--artifact-root", str(artifacts),
        ]
        for predicate in PREDICATES:
            direct += ["--predicate", predicate]
        direct += [
            "--transport", "interactive",
            "--inline-reason", "atomic-direct",
            "--tracking", "untracked",
            "--spec-read", "not-applicable",
            "--drift-verdict", "no-project-spec",
            "--workflow-mode", "untracked",
            "--artifact-guard", "preflight-passed",
        ]
        compiled = subprocess.run(direct, text=True, capture_output=True)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        route_id = json.loads(compiled.stdout)["route_id"]
        route = artifacts / ".runtime" / "routes" / f"{route_id}.json"
        bound = subprocess.run(
            [
                sys.executable, str(GUARD), "--agent-home", str(self.home),
                "bind", "--route", str(route), "--cwd", str(project),
                "--session", "research-session",
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(bound.returncode, 0, bound.stderr)
        self.opportunity("research-session", cwd=project)
        allowed = subprocess.run(
            [
                sys.executable, str(GUARD), "--agent-home", str(self.home),
                "check", "--tool", "ArtifactWrite", "--file", str(target),
                "--cwd", str(project), "--session", "research-session",
            ],
            text=True,
            capture_output=True,
            env={**os.environ, "MEM_RECALL_RECEIPTS": str(self.receipts)},
        )
        self.assertEqual(allowed.returncode, 0, allowed.stderr)

    def test_bound_route_requires_current_turn_recall_opportunity(self) -> None:
        self.assertEqual(self.bind().returncode, 0)
        missing = self.guard(
            "--tool", "Edit", "--file", str(self.repo / "app.py"),
            "--turn", "turn-a", opportunity=False,
        )
        self.assertEqual(missing.returncode, 2)
        self.assertIn("recall-opportunity-missing", missing.stderr)

        self.opportunity(turn="turn-a")
        allowed = self.guard(
            "--tool", "Edit", "--file", str(self.repo / "app.py"),
            "--turn", "turn-a", opportunity=False,
        )
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        foreign_turn = self.guard(
            "--tool", "Edit", "--file", str(self.repo / "app.py"),
            "--turn", "turn-b", opportunity=False,
        )
        self.assertEqual(foreign_turn.returncode, 2)
        self.assertIn("recall-opportunity-turn-mismatch", foreign_turn.stderr)

        self.opportunity(
            turn="turn-a",
            created_at_ns=time.time_ns() - 2 * 24 * 60 * 60 * 1_000_000_000,
        )
        stale = self.guard(
            "--tool", "Edit", "--file", str(self.repo / "app.py"),
            "--turn", "turn-a", opportunity=False,
        )
        self.assertEqual(stale.returncode, 2)
        self.assertIn("recall-opportunity-stale", stale.stderr)

        self.opportunity(source="explicit-skip")
        recovered = self.guard(
            "--tool", "Edit", "--file", str(self.repo / "app.py"),
            "--turn", "turn-c", opportunity=False,
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr)

    def test_full_width_probe_receipt_is_accepted_and_overflow_denied(self) -> None:
        # Regression: the probe was widened to CANDIDATE_MAX_RESULTS=6 while
        # this guard still capped result_ids at 3, so every explicit-recall
        # receipt that actually carried hits was rejected as results-invalid
        # and only evidence-free skip receipts could pass.
        self.assertEqual(self.bind().returncode, 0)
        full = [f"record-{index}" for index in range(6)]
        self.opportunity(turn="turn-a", source="explicit-recall", result_ids=full)
        allowed = self.guard(
            "--tool", "Edit", "--file", str(self.repo / "app.py"),
            "--turn", "turn-a", opportunity=False,
        )
        self.assertEqual(allowed.returncode, 0, allowed.stderr)

        self.opportunity(
            turn="turn-a", source="explicit-recall",
            result_ids=[f"record-{index}" for index in range(7)],
        )
        overflow = self.guard(
            "--tool", "Edit", "--file", str(self.repo / "app.py"),
            "--turn", "turn-a", opportunity=False,
        )
        self.assertEqual(overflow.returncode, 2)
        self.assertIn("recall-opportunity-results-invalid", overflow.stderr)

    def test_linked_worktree_route_allows_only_primary_canonical_artifacts(self) -> None:
        linked = self.base / "linked"
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "add", "-q", str(linked), "HEAD"],
            check=True,
        )
        artifact_root = self.repo / ".agent_reports"
        route = artifact_root / ".runtime" / "routes" / "linked.json"
        route.parent.mkdir(parents=True)
        command = [
            sys.executable, str(ROUTER), "compile",
            "--slug", "linked-worktree-fixture",
            "--capability", "autopilot-code",
            "--capability-mode", "dev",
            "--intensity", "direct",
            "--cwd", str(linked),
            "--artifact-root", str(artifact_root),
        ]
        for predicate in PREDICATES:
            command += ["--predicate", predicate]
        command += [
            "--transport", "interactive",
            "--inline-reason", "atomic-direct",
            "--tracking", "untracked",
            "--spec-read", "not-applicable",
            "--drift-verdict", "no-project-spec",
            "--workflow-mode", "untracked",
            "--artifact-guard", "preflight-passed",
        ]
        compiled = subprocess.run(command, text=True, capture_output=True)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        route_id = json.loads(compiled.stdout)["route_id"]
        route = artifact_root / ".runtime" / "routes" / f"{route_id}.json"
        bound = subprocess.run(
            [
                sys.executable, str(GUARD), "--agent-home", str(self.home),
                "bind", "--route", str(route), "--cwd", str(linked),
                "--session", "linked-session",
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(bound.returncode, 0, bound.stderr)
        self.opportunity("linked-session", cwd=linked)
        target = artifact_root / "plans" / "runtime" / "plan.md"
        allowed = subprocess.run(
            [
                sys.executable, str(GUARD), "--agent-home", str(self.home),
                "check", "--tool", "ArtifactWrite", "--file", str(target),
                "--cwd", str(artifact_root), "--session", "linked-session",
            ],
            text=True,
            capture_output=True,
            env={**os.environ, "MEM_RECALL_RECEIPTS": str(self.receipts)},
        )
        self.assertEqual(allowed.returncode, 0, allowed.stderr)

        foreign = self.base / "foreign" / ".agent_reports" / "plans" / "plan.md"
        denied = subprocess.run(
            [
                sys.executable, str(GUARD), "--agent-home", str(self.home),
                "check", "--tool", "ArtifactWrite", "--file", str(foreign),
                "--cwd", str(foreign.parent), "--session", "linked-session",
            ],
            text=True,
            capture_output=True,
            env={**os.environ, "MEM_RECALL_RECEIPTS": str(self.receipts)},
        )
        self.assertEqual(denied.returncode, 2)
        self.assertIn("route-artifact-root-mismatch", denied.stderr)

    def test_claude_transcript_turn_anchor_tracks_latest_real_user_uuid(self) -> None:
        transcript = self.base / "transcript.jsonl"
        transcript.write_text(
            "\n".join([
                json.dumps({"type": "user", "uuid": "user-one", "message": {"role": "user", "content": "one"}}),
                json.dumps({"type": "assistant", "uuid": "assistant-one", "message": {"role": "assistant", "content": "reply"}}),
            ]) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(
            MATERIAL_GUARD.transcript_turn_id(str(transcript)),
            "transcript-user:user-one",
        )
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "type": "user", "uuid": "user-two", "message": {"role": "user", "content": "two"},
            }) + "\n")
        self.assertEqual(
            MATERIAL_GUARD.transcript_turn_id(str(transcript)),
            "transcript-user:user-two",
        )
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "type": "assistant", "uuid": "assistant-tool",
                "message": {"role": "assistant", "content": [{
                    "type": "tool_use", "id": "tool-one", "name": "Read",
                }]},
            }) + "\n")
            handle.write(json.dumps({
                "type": "user", "uuid": "tool-result-one",
                "message": {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": "tool-one", "content": "result",
                }]},
            }) + "\n")
        self.assertEqual(
            MATERIAL_GUARD.transcript_turn_id(str(transcript)),
            "transcript-user:user-two",
        )

    def test_claude_lookup_then_two_edits_keep_same_turn_receipt(self) -> None:
        session = "same-turn-edits"
        transcript = self.base / "same-turn.jsonl"
        transcript.write_text(json.dumps({
            "type": "user", "uuid": "actual-user-prompt",
            "message": {"role": "user", "content": "inspect and fix"},
        }) + "\n", encoding="utf-8")
        self.assertEqual(self.bind(session).returncode, 0)
        self.opportunity(session, turn="transcript-user:actual-user-prompt")

        clean = {
            key: value for key, value in os.environ.items()
            if key not in {"AGENT_ROUTE_FILE", "AGENT_ROUTE_ID", "AGENT_ROUTE_NODE"}
        }

        def edit_after(tool_id: str, tool_name: str) -> subprocess.CompletedProcess[str]:
            with transcript.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "type": "assistant", "uuid": f"assistant-{tool_id}",
                    "message": {"role": "assistant", "content": [{
                        "type": "tool_use", "id": tool_id, "name": tool_name,
                    }]},
                }) + "\n")
                handle.write(json.dumps({
                    "type": "user", "uuid": f"result-{tool_id}",
                    "message": {"role": "user", "content": [{
                        "type": "tool_result", "tool_use_id": tool_id, "content": "ok",
                    }]},
                }) + "\n")
            payload = {
                "hook_event_name": "PreToolUse", "tool_name": "Edit",
                "tool_input": {"file_path": str(self.repo / "app.py")},
                "cwd": str(self.repo), "session_id": session,
                "transcript_path": str(transcript),
            }
            return subprocess.run(
                [sys.executable, str(GUARD)], input=json.dumps(payload), text=True,
                capture_output=True,
                env={
                    **clean, "AGENT_HOME": str(self.home),
                    "MEM_RECALL_RECEIPTS": str(self.receipts),
                },
            )

        first_edit = edit_after("lookup-one", "Read")
        self.assertEqual(first_edit.returncode, 0, first_edit.stderr)
        self.assertEqual(first_edit.stdout, "", first_edit.stdout)
        second_edit = edit_after("edit-one", "Edit")
        self.assertEqual(second_edit.returncode, 0, second_edit.stderr)
        self.assertEqual(second_edit.stdout, "", second_edit.stdout)

    def test_docs_config_scratch_and_foreign_session_behavior(self) -> None:
        config_script = self.repo / "config" / "bootstrap.sh"
        config_script.parent.mkdir()
        config_script.write_text("true\n", encoding="utf-8")
        for path in (
            self.repo / "README.md",
            self.repo / "settings.json",
            config_script,
            self.base / "scratch.py",
        ):
            result = self.guard("--tool", "Write", "--file", str(path))
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.bind("session-a").returncode, 0)
        denied = self.guard(
            "--tool", "Edit", "--file", str(self.repo / "app.py"), session="session-b"
        )
        self.assertEqual(denied.returncode, 2)

    def test_stale_source_commit_and_tampered_record_are_denied(self) -> None:
        self.assertEqual(self.bind().returncode, 0)
        source = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True).stdout.strip()
        # SD-67: a first-parent descendant HEAD is mid-cycle progress — the
        # route stays valid across the cycle's own commits.
        subprocess.run(["git", "-C", str(self.repo), "commit", "--allow-empty", "-qm", "advance"], check=True)
        advanced = self.guard("--tool", "Edit", "--file", str(self.repo / "app.py"))
        self.assertEqual(advanced.returncode, 0, advanced.stderr)
        # Rewritten history is not a descendant: still stale.
        subprocess.run(["git", "-C", str(self.repo), "reset", "--hard", "-q", "HEAD^"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "--allow-empty", "--amend", "-qm", "rewritten"], check=True)
        stale = self.guard("--tool", "Edit", "--file", str(self.repo / "app.py"))
        self.assertEqual(stale.returncode, 2)
        self.assertIn("route-source-commit-stale", stale.stderr)
        subprocess.run(["git", "-C", str(self.repo), "reset", "--hard", "-q", source], check=True)
        value = json.loads(self.route.read_text())
        value["route_id"] = "rt-tampered"
        self.route.write_text(json.dumps(value), encoding="utf-8")
        tampered = self.guard("--tool", "Edit", "--file", str(self.repo / "app.py"))
        self.assertEqual(tampered.returncode, 2)
        self.assertIn("route-record-verification-failed", tampered.stderr)

    def test_crashing_verifier_is_not_reported_as_a_bad_route_record(self) -> None:
        """A half-written verifier is not evidence that the record is invalid.

        `~/.claude/utilities` symlinks back to the repo, so a parallel session
        editing the harness can be observed mid-write.  Reporting that as
        `route-record-verification-failed` sent one debugging pass chasing a route
        record that was in fact correct (2026-08-04).
        """
        self.assertEqual(self.bind().returncode, 0)
        verifier = self.home / "utilities" / "capability-route.py"
        intact = verifier.read_text()
        # A truncated module: imports fine, dies at run time — exactly the torn read.
        verifier.write_text("import sys\nraise NameError('torn read')\n", encoding="utf-8")
        try:
            crashed = self.guard("--tool", "Edit", "--file", str(self.repo / "app.py"))
            self.assertEqual(crashed.returncode, 2)
            self.assertIn("route-verifier-crashed", crashed.stderr)
            self.assertNotIn("route-record-verification-failed", crashed.stderr)
        finally:
            verifier.write_text(intact, encoding="utf-8")
        # The same record verifies once the file is whole again — no retry poisoning.
        self.assertEqual(
            self.guard("--tool", "Edit", "--file", str(self.repo / "app.py")).returncode, 0)

    def test_route_symlink_is_not_accepted_as_authority(self) -> None:
        linked_route = self.artifacts / "linked-route.json"
        linked_route.symlink_to(self.route)
        result = subprocess.run(
            [
                sys.executable, str(GUARD), "--agent-home", str(self.home),
                "bind", "--route", str(linked_route), "--cwd", str(self.repo),
                "--session", "linked-route-session",
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("route-file-unsafe", result.stderr)

    def test_worker_route_environment_is_valid_without_session_marker(self) -> None:
        allowed = self.guard(
            "--tool", "Edit", "--file", str(self.repo / "app.py"),
            session="worker-session",
            env={
                "AGENT_ROUTE_FILE": str(self.route),
                "AGENT_ROUTE_ID": self.route_id,
                "AGENT_ROUTE_NODE": "inline",
            },
        )
        self.assertEqual(allowed.returncode, 0, allowed.stderr)

    def test_commit_chokepoint_source_docs_rename_and_all(self) -> None:
        (self.repo / "app.py").write_text("print('two')\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "app.py"], check=True)
        denied = self.guard("--tool", "Bash", "--command", "git commit -m source")
        self.assertEqual(denied.returncode, 2)
        self.assertEqual(self.bind().returncode, 0)
        self.assertEqual(
            self.guard("--tool", "Bash", "--command", "git commit -m source").returncode,
            0,
        )

        self.reset()
        (self.repo / "README.md").write_text("two\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "README.md"], check=True)
        self.assertEqual(
            self.guard("--tool", "Bash", "--command", "git commit -m docs", session="fresh").returncode,
            0,
        )

        self.reset()
        subprocess.run(["git", "-C", str(self.repo), "mv", "app.py", "renamed.py"], check=True)
        self.assertEqual(
            self.guard("--tool", "Bash", "--command", "git commit -m rename", session="fresh").returncode,
            0,
        )

        self.reset()
        (self.repo / "app.py").write_text("print('three')\n", encoding="utf-8")
        denied_all = self.guard(
            "--tool", "Bash", "--command", "git commit -am tracked", session="fresh"
        )
        self.assertEqual(denied_all.returncode, 2)
        nested = self.guard(
            "--tool", "Bash", "--command", "bash -c 'git commit -am nested'", session="fresh"
        )
        self.assertEqual(nested.returncode, 2)

        self.reset()
        (self.repo / "app.py").write_text("print('staged elsewhere')\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "app.py"], check=True)
        (self.repo / "README.md").write_text("docs only target\n", encoding="utf-8")
        only_docs = self.guard(
            "--tool", "Bash", "--command", "git commit --only=README.md -m docs",
            session="fresh",
        )
        self.assertEqual(only_docs.returncode, 0, only_docs.stderr)

        only_source = self.guard(
            "--tool", "Bash", "--command", "git commit --only=app.py -m source",
            session="fresh",
        )
        self.assertEqual(only_source.returncode, 2)

    def test_new_source_file_in_repo_is_material(self) -> None:
        denied = self.guard(
            "--tool", "Write", "--file", str(self.repo / "new" / "feature.py")
        )
        self.assertEqual(denied.returncode, 2)

    def test_posttool_compile_binds_and_session_end_clears(self) -> None:
        spoof_payload = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {
                "command": f"echo {ROUTER} compile --output {self.route}"
            },
            "cwd": str(self.repo),
            "session_id": "spoof-session",
        }
        subprocess.run(
            [sys.executable, str(GUARD)], input=json.dumps(spoof_payload), text=True,
            capture_output=True, env={**os.environ, "AGENT_HOME": str(self.home)}, check=True,
        )
        spoofed = self.guard(
            "--tool", "Edit", "--file", str(self.repo / "app.py"), session="spoof-session"
        )
        self.assertEqual(spoofed.returncode, 2)

        command = f"python3 {ROUTER} compile --output {self.route}"
        payload = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": str(self.repo),
            "session_id": "hook-session",
        }
        result = subprocess.run(
            [sys.executable, str(GUARD)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env={**os.environ, "AGENT_HOME": str(self.home)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        allowed = self.guard(
            "--tool", "Edit", "--file", str(self.repo / "app.py"), session="hook-session"
        )
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        end_payload = {"hook_event_name": "SessionEnd", "session_id": "hook-session"}
        subprocess.run(
            [sys.executable, str(GUARD)], input=json.dumps(end_payload), text=True,
            capture_output=True, env={**os.environ, "AGENT_HOME": str(self.home)}, check=True,
        )
        denied = self.guard(
            "--tool", "Edit", "--file", str(self.repo / "app.py"), session="hook-session"
        )
        self.assertEqual(denied.returncode, 2)

    def _compile_command(self) -> list[str]:
        command = [
            sys.executable, str(ROUTER), "compile",
            "--slug", "compile-command-fixture",
            "--capability", "autopilot-code",
            "--capability-mode", "dev",
            "--intensity", "direct",
            "--cwd", str(self.repo),
            "--artifact-root", str(self.artifacts),
        ]
        for predicate in PREDICATES:
            command += ["--predicate", predicate]
        command += [
            "--transport", "interactive",
            "--inline-reason", "atomic-direct",
            "--tracking", "untracked",
            "--spec-read", "not-applicable",
            "--drift-verdict", "no-project-spec",
            "--workflow-mode", "untracked",
            "--artifact-guard", "preflight-passed",
        ]
        return command

    def test_posttool_compile_without_output_binds_via_stdout_route_id(self) -> None:
        command = self._compile_command()
        result = subprocess.run(command, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        route_id = json.loads(result.stdout)["route_id"]
        canonical = self.artifacts / ".runtime" / "routes" / f"{route_id}.json"
        self.assertTrue(canonical.is_file())

        shell_command = " ".join(shlex.quote(part) for part in command)
        payload = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": shell_command},
            "tool_response": {"stdout": result.stdout, "stderr": result.stderr},
            "cwd": str(self.repo),
            "session_id": "stdout-session",
        }
        hook_result = subprocess.run(
            [sys.executable, str(GUARD)], input=json.dumps(payload), text=True,
            capture_output=True, env={**os.environ, "AGENT_HOME": str(self.home)},
        )
        self.assertEqual(hook_result.returncode, 0, hook_result.stderr)
        allowed = self.guard(
            "--tool", "Edit", "--file", str(self.repo / "app.py"), session="stdout-session"
        )
        self.assertEqual(allowed.returncode, 0, allowed.stderr)

    def test_posttool_compile_without_output_and_without_stdout_does_not_bind(self) -> None:
        command = self._compile_command()
        shell_command = " ".join(shlex.quote(part) for part in command)
        payload = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": shell_command},
            "cwd": str(self.repo),
            "session_id": "no-stdout-session",
        }
        hook_result = subprocess.run(
            [sys.executable, str(GUARD)], input=json.dumps(payload), text=True,
            capture_output=True, env={**os.environ, "AGENT_HOME": str(self.home)},
        )
        self.assertEqual(hook_result.returncode, 0, hook_result.stderr)
        denied = self.guard(
            "--tool", "Edit", "--file", str(self.repo / "app.py"), session="no-stdout-session"
        )
        self.assertEqual(denied.returncode, 2)

    def test_codex_preflight_compile_binds_but_echo_and_foreign_paths_do_not(self) -> None:
        trusted = str(ROOT / "adapters" / "codex" / "bin" / "preflight.sh")
        for session, command in (
            ("codex-echo", f"echo {trusted} route --capability autopilot-code --output {self.route}"),
            ("codex-foreign", f"/tmp/preflight.sh route --capability autopilot-code --output {self.route}"),
        ):
            payload = {"hook_event_name": "PostToolUse", "tool_name": "Bash",
                       "tool_input": {"command": command}, "cwd": str(self.repo),
                       "session_id": session}
            subprocess.run([sys.executable, str(GUARD)], input=json.dumps(payload),
                           text=True, capture_output=True,
                           env={**os.environ, "AGENT_HOME": str(self.home)}, check=True)
            self.assertEqual(self.guard("--tool", "Edit", "--file", str(self.repo / "app.py"),
                                        session=session).returncode, 2)
        payload = {"hook_event_name": "PostToolUse", "tool_name": "functions.exec_command",
                   "tool_input": {"command": f"{trusted} route --capability autopilot-code --output {self.route}"},
                   "cwd": str(self.repo), "session_id": "codex-good"}
        result = subprocess.run([sys.executable, str(GUARD)], input=json.dumps(payload), text=True,
                                capture_output=True, env={**os.environ, "AGENT_HOME": str(self.home)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.guard("--tool", "Edit", "--file", str(self.repo / "app.py"),
                                    session="codex-good").returncode, 0)

    def test_codex_compile_binding_requires_one_exact_local_output_and_proof(self) -> None:
        trusted = str(ROOT / "adapters" / "codex" / "bin" / "preflight.sh")

        def post(command: str, *, cwd: Path = self.repo, session: str = "compile-case"):
            payload = {"hook_event_name": "PostToolUse", "tool_name": "functions.exec_command",
                       "tool_input": {"command": command}, "cwd": str(cwd), "session_id": session}
            return subprocess.run([sys.executable, str(GUARD)], input=json.dumps(payload), text=True,
                                  capture_output=True, env={**os.environ, "AGENT_HOME": str(self.home)})

        def denied(session: str = "compile-case"):
            result = self.guard("--tool", "Edit", "--file", str(self.repo / "app.py"), session=session)
            self.assertEqual(result.returncode, 2, result.stderr)

        for command in (
            f"{trusted} route --capability autopilot-code",
            f"{trusted} route --capability autopilot-code --output {self.route} --output {self.artifacts / 'second.json'}",
        ):
            session = "compile-" + str(abs(hash(command)))
            result = post(command, session=session)
            self.assertEqual(result.returncode, 0, result.stderr)
            denied(session)

        result = post(f"{trusted} route --capability autopilot-code --output={self.route}", session="equals-output")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.guard("--tool", "Edit", "--file", str(self.repo / "app.py"),
                                    session="equals-output").returncode, 0)

        # An exact shell directory change is supported only when it still binds
        # the route against the payload cwd; foreign cwd/session proofs fail.
        result = post(f"cd {self.repo} && {trusted} route --capability autopilot-code --output {self.route}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.guard("--tool", "Edit", "--file", str(self.repo / "app.py"),
                                    session="compile-case").returncode, 0)
        post(f"{trusted} route --capability autopilot-code --output {self.route}", cwd=self.base,
             session="foreign-cwd")
        denied("foreign-cwd")
        denied("foreign-session")

        value = json.loads(self.route.read_text())
        value["route_id"] = "rt-tampered"
        self.route.write_text(json.dumps(value))
        post(f"{trusted} route --capability autopilot-code --output {self.route}", session="tampered")
        denied("tampered")

    def test_codex_wrapper_requires_shared_git_common_dir_and_tracks_effective_cwd(self) -> None:
        canonical = self.base / "canonical"
        ignored = shutil.ignore_patterns(".git", ".agent_reports", ".claude_reports", ".dispatch", ".spec-grounding", "__pycache__")
        shutil.copytree(ROOT, canonical, symlinks=True, ignore=ignored)
        subprocess.run(["git", "init", "-q", str(canonical)], check=True)
        subprocess.run(["git", "-C", str(canonical), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(canonical), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(canonical), "add", "."], check=True)
        subprocess.run(["git", "-C", str(canonical), "commit", "-qm", "exact committed fixture"], check=True)
        linked = self.base / "linked"
        subprocess.run(["git", "-C", str(canonical), "worktree", "add", "-q", str(linked), "HEAD"], check=True)

        foreign = self.base / "foreign"
        (foreign / "adapters/codex/bin").mkdir(parents=True)
        shutil.copy2(canonical / "adapters/codex/bin/preflight.sh", foreign / "adapters/codex/bin/preflight.sh")
        subprocess.run(["git", "init", "-q", str(foreign)], check=True)
        subprocess.run(["git", "-C", str(foreign), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(foreign), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(foreign), "add", "."], check=True)
        subprocess.run(["git", "-C", str(foreign), "commit", "-qm", "foreign wrapper"], check=True)

        fixture_guard_path = canonical / "hooks/material-route-guard.py"
        fixture_spec = importlib.util.spec_from_file_location("fixture_material_route_guard", fixture_guard_path)
        assert fixture_spec and fixture_spec.loader
        fixture_guard = importlib.util.module_from_spec(fixture_spec)
        fixture_spec.loader.exec_module(fixture_guard)
        wrapper_rel = "adapters/codex/bin/preflight.sh"

        # The canonical installed harness remains trusted even when it is a
        # release tree without Git metadata. Git common-dir proof is required
        # only to extend that trust to a sibling linked worktree.
        standalone = self.base / "standalone"
        (standalone / "adapters/codex/bin").mkdir(parents=True)
        shutil.copy2(
            canonical / wrapper_rel,
            standalone / wrapper_rel,
        )
        original_root = fixture_guard.ROOT
        fixture_guard.ROOT = standalone
        try:
            standalone_invocations = fixture_guard.route_compile_invocations(
                f"{wrapper_rel} route --capability autopilot-code --output route.json",
                standalone,
            )
        finally:
            fixture_guard.ROOT = original_root
        self.assertEqual(len(standalone_invocations), 1)
        self.assertEqual(
            standalone_invocations[0].outputs,
            ((standalone / "route.json").resolve(),),
        )

        def compile_command(target: Path, output: str | None = None) -> str:
            args = [
                wrapper_rel, "route", "--capability", "autopilot-code",
                "--capability-mode", "dev", "--slug", "wrapper-fixture", "--intensity", "direct",
                "--cwd", str(target), "--artifact-root", str(canonical),
            ]
            for predicate in PREDICATES:
                args += ["--predicate", predicate]
            args += [
                "--transport", "interactive", "--inline-reason", "atomic-direct",
                "--tracking", "untracked", "--spec-read", "not-applicable",
                "--drift-verdict", "no-project-spec", "--workflow-mode", "untracked",
                "--artifact-guard", "preflight-passed",
            ]
            if output is not None:
                args += ["--output", output]
            return shlex.join(args)

        def run_bridge(command: str, target: Path, session: str) -> subprocess.CompletedProcess[str]:
            bridge = canonical / "adapters/codex/hooks/posttooluse-read-marker.py"
            payload = {
                "hook_event_name": "PostToolUse", "tool_name": "functions.exec_command",
                "tool_input": {"command": command}, "cwd": str(target), "session_id": session,
            }
            return subprocess.run(
                [sys.executable, str(bridge)], input=json.dumps(payload), text=True,
                capture_output=True, env={**os.environ, "AGENT_HOME": str(canonical)},
            )

        def run_portable(command: str, target: Path, session: str) -> subprocess.CompletedProcess[str]:
            payload = {
                "hook_event_name": "PostToolUse", "tool_name": "functions.exec_command",
                "tool_input": {"command": command}, "cwd": str(target), "session_id": session,
            }
            return subprocess.run(
                [sys.executable, str(fixture_guard_path)], input=json.dumps(payload), text=True,
                capture_output=True, env={**os.environ, "AGENT_HOME": str(canonical)},
            )

        cases = (
            ("canonical-absolute", canonical, str(canonical / wrapper_rel)),
            ("canonical-relative", canonical, wrapper_rel),
            ("linked-absolute", linked, str(linked / wrapper_rel)),
            ("linked-relative", linked, wrapper_rel),
        )
        for name, target, executable in cases:
            with self.subTest(case=name):
                # Execute route compilation through the canonical launch root.
                # The command handed to the post-tool parser still names the
                # trusted linked wrapper, which is the behavior under test.
                probe_command = compile_command(target).replace(
                    wrapper_rel, str(canonical / wrapper_rel), 1
                )
                fixture_env = {**os.environ, "AGENT_HOME": str(canonical)}
                probe = subprocess.run(
                    probe_command, shell=True, cwd=target, text=True,
                    capture_output=True, env=fixture_env,
                )
                self.assertEqual(probe.returncode, 0, probe.stderr)
                route_id = json.loads(probe.stdout)["route_id"]
                output = str(canonical / ".runtime" / "routes" / f"{route_id}.json")
                command = compile_command(target, output).replace(wrapper_rel, executable, 1)
                self.assertEqual(
                    fixture_guard.route_compile_invocations(command, target)[0].effective_cwd,
                    target.resolve(),
                )
                for bridge_name, runner in (("portable", run_portable), ("codex", run_bridge)):
                    session = f"{name}-{bridge_name}"
                    bound = runner(command, target, session)
                    self.assertEqual(bound.returncode, 0, bound.stderr)
                    marker_file = fixture_guard.marker_path(canonical, session)
                    self.assertTrue(marker_file.is_file(), session)
                    marker = json.loads(marker_file.read_text())
                    sealed_route = json.loads(Path(marker["route_file"]).read_text())
                    self.assertEqual(Path(sealed_route["cwd"]).resolve(), target.resolve())
                    self.opportunity(session, cwd=target)
                    allowed = subprocess.run(
                        [sys.executable, str(fixture_guard_path), "--agent-home", str(canonical), "check", "--tool", "Edit",
                         "--file", str(target / "app.py"), "--cwd", str(target), "--session", session,
                         ], text=True, capture_output=True,
                        env={**os.environ, "MEM_RECALL_RECEIPTS": str(self.receipts)},
                    )
                    self.assertEqual(allowed.returncode, 0, allowed.stderr)

        cd_probe_command = (
            f"cd {linked} && "
            + compile_command(linked).replace(wrapper_rel, str(canonical / wrapper_rel), 1)
        )
        fixture_env = {**os.environ, "AGENT_HOME": str(canonical)}
        cd_probe = subprocess.run(
            cd_probe_command, shell=True, cwd=canonical, text=True,
            capture_output=True, env=fixture_env,
        )
        self.assertEqual(cd_probe.returncode, 0, cd_probe.stderr)
        cd_route_id = json.loads(cd_probe.stdout)["route_id"]
        cd_command = (
            f"cd {linked} && "
            + compile_command(
                linked,
                str(canonical / ".runtime" / "routes" / f"{cd_route_id}.json"),
            )
        )
        result = run_portable(cd_command, canonical, "preceding-cd-portable")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(fixture_guard.marker_path(canonical, "preceding-cd-portable").is_file())
        result = run_bridge(cd_command, canonical, "preceding-cd-codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(fixture_guard.marker_path(canonical, "preceding-cd-codex").is_file())
        for session in ("preceding-cd-portable", "preceding-cd-codex"):
            self.opportunity(session, cwd=linked)
            allowed = subprocess.run(
                [sys.executable, str(fixture_guard_path), "--agent-home", str(canonical), "check", "--tool", "Edit",
                 "--file", str(linked / "app.py"), "--cwd", str(linked), "--session", session,
                ], text=True, capture_output=True,
                env={**os.environ, "MEM_RECALL_RECEIPTS": str(self.receipts)},
            )
            self.assertEqual(allowed.returncode, 0, allowed.stderr)

        no_bind = (
            ("foreign-repository", str(foreign / wrapper_rel), foreign),
            ("missing-file", str(canonical / "adapters/codex/bin/missing.sh"), canonical),
            ("echo-substring", f"echo {canonical / wrapper_rel} route --capability autopilot-code --output route.json", canonical),
            ("wrong-subcommand", f"{canonical / wrapper_rel} status --capability autopilot-code --output route.json", canonical),
            ("missing-capability", f"{canonical / wrapper_rel} route autopilot-code --output route.json", canonical),
            ("direct-non-compile", f"{canonical / wrapper_rel} status", canonical),
            ("zero-output", f"{canonical / wrapper_rel} route --capability autopilot-code", canonical),
            ("multiple-output", f"{canonical / wrapper_rel} route --capability autopilot-code --output one.json --output two.json", canonical),
        )
        for name, command, cwd in no_bind:
            with self.subTest(no_bind=name):
                invocations = fixture_guard.route_compile_invocations(command, cwd)
                if name == "multiple-output":
                    self.assertEqual(len(invocations), 1)
                    self.assertEqual(len(invocations[0].outputs), 2)
                else:
                    self.assertEqual(invocations, [])

    def test_claude_hook_protocol_and_source_registration(self) -> None:
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Edit",
            "tool_input": {"file_path": str(self.repo / "app.py")},
            "cwd": str(self.repo),
            "session_id": "unbound-hook-session",
        }
        denied = subprocess.run(
            [sys.executable, str(GUARD)], input=json.dumps(payload), text=True,
            capture_output=True, env={**os.environ, "AGENT_HOME": str(self.home)},
        )
        self.assertEqual(denied.returncode, 0, denied.stderr)
        decision = json.loads(denied.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["hookEventName"], "PreToolUse")
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("silent no-route", decision["permissionDecisionReason"])

        notebook_payload = {
            **payload,
            "tool_name": "NotebookEdit",
            "tool_input": {"notebook_path": str(self.repo / "analysis.ipynb")},
        }
        notebook_denied = subprocess.run(
            [sys.executable, str(GUARD)], input=json.dumps(notebook_payload), text=True,
            capture_output=True, env={**os.environ, "AGENT_HOME": str(self.home)},
        )
        self.assertEqual(notebook_denied.returncode, 0, notebook_denied.stderr)
        self.assertEqual(
            json.loads(notebook_denied.stdout)["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )

        settings = json.loads((ROOT / "adapters" / "claude" / "settings.json").read_text())
        expected = {
            "PreToolUse": {"Edit|Write|MultiEdit|NotebookEdit", "Bash"},
            "PostToolUse": {"Bash"},
            "SessionEnd": {"*"},
        }
        for event, matchers in expected.items():
            observed = {
                group.get("matcher")
                for group in settings["hooks"][event]
                if any(
                    "material-route-guard.py" in hook.get("command", "")
                    for hook in group.get("hooks", [])
                )
            }
            self.assertEqual(observed, matchers)
        projection = ROOT / "adapters" / "claude" / "hooks" / "material-route-guard.py"
        self.assertTrue(projection.is_symlink())
        self.assertEqual(projection.resolve(), GUARD.resolve())


    def test_j_closed_route_marker_refuses_material_edit(self):
        # Defect J (rt-a2d042ad): edits kept flowing under an already-closed route's
        # bind marker. The route record is immutable and says nothing about closure,
        # so the closure sidecar must be the gate.
        self.assertEqual(self.bind().returncode, 0)
        self.assertEqual(self.guard("--tool", "Edit", "--file", str(self.repo / "app.py")).returncode, 0)
        self.close_route()
        denied = self.guard("--tool", "Edit", "--file", str(self.repo / "app.py"))
        self.assertEqual(denied.returncode, 2, denied.stdout + denied.stderr)
        self.assertIn("reason=route-closed", denied.stdout + denied.stderr)
        # A material commit under the same dead marker is refused for the same reason.
        (self.repo / "app.py").write_text("print('two')\n", encoding="utf-8")
        commit = self.guard(
            "--tool", "Bash",
            "--command", "git -C %s commit -am wip" % shlex.quote(str(self.repo)),
        )
        self.assertEqual(commit.returncode, 2, commit.stdout + commit.stderr)
        self.assertIn("reason=route-closed", commit.stdout + commit.stderr)

    def test_j_closed_route_cannot_be_bound(self):
        # Binding a route that is already closed is refused up front, so the marker
        # never exists; a later write then fails as a plain missing route.
        self.close_route()
        bound = self.bind()
        self.assertNotEqual(bound.returncode, 0)
        self.assertIn("route-closed", bound.stdout + bound.stderr)

    def test_j_registered_worker_route_is_not_closed_gated(self):
        # A worker's route lifetime is the dispatch contract's; a racing owner close
        # must not refuse a worker mid-stage.
        self.close_route()
        allowed = self.guard(
            "--tool", "Edit", "--file", str(self.repo / "app.py"),
            env={
                "AGENT_ROUTE_FILE": str(self.route),
                "AGENT_ROUTE_ID": self.route_id,
                "AGENT_ROUTE_NODE": "inline",
            },
            opportunity=False,
        )
        self.assertEqual(allowed.returncode, 0, allowed.stdout + allowed.stderr)

    def test_j_symlinked_outcome_sidecar_is_not_closure_evidence(self):
        # The sidecar is trusted, so it must be a real file in the canonical route
        # dir -- a symlink planted there is not closure evidence (same rule the
        # route record itself follows).
        self.assertEqual(self.bind().returncode, 0)
        other = self.base / "foreign-outcome.json"
        other.write_text("{}", encoding="utf-8")
        self.route.with_name(self.route.stem + ".outcome.json").symlink_to(other)
        allowed = self.guard("--tool", "Edit", "--file", str(self.repo / "app.py"))
        self.assertEqual(allowed.returncode, 0, allowed.stdout + allowed.stderr)


class ArtifactBucketCapsTest(unittest.TestCase):
    """W7C: one bucket table gates legacy, cycle, and shared layouts."""

    def test_legacy_bucket(self):
        self.assertEqual(MATERIAL_GUARD.artifact_bucket_caps(("plans", "c", "plan.md")), {"autopilot-code", "audit"})
        self.assertEqual(MATERIAL_GUARD.artifact_bucket_caps(("designs", "c", "x.md")), {"autopilot-design", "audit"})
        self.assertIsNone(MATERIAL_GUARD.artifact_bucket_caps(("notes", "x.md")))

    def test_cycle_layout_maps_bucket_after_artifacts(self):
        parts = ("campaigns", "camp_" + "1" * 32, "cycles", "cyc_" + "2" * 32, "artifacts", "spec", "prd.md")
        self.assertEqual(MATERIAL_GUARD.artifact_bucket_caps(parts), MATERIAL_GUARD.CAPABILITY_ARTIFACT_CAPS["spec"])
        self.assertIsNone(MATERIAL_GUARD.artifact_bucket_caps(("campaigns", "camp_x", "campaign.json")))
        self.assertIsNone(MATERIAL_GUARD.artifact_bucket_caps(parts[:5] + ("unknown-bucket", "x")))

    def test_shared_layout_maps_kind(self):
        self.assertEqual(MATERIAL_GUARD.artifact_bucket_caps(("shared", "analysis", "ref_x", "revisions", "rrev_y", "a.md")),
                         MATERIAL_GUARD.CAPABILITY_ARTIFACT_CAPS["analysis_project"])
        self.assertIsNone(MATERIAL_GUARD.artifact_bucket_caps(("shared", "plans", "ref_x")))

    def test_capability_artifact_caps_path_entry(self):
        path = Path("/tmp/proj/.agent_reports/campaigns/camp_1/cycles/cyc_2/artifacts/research/topic/report.md")
        self.assertEqual(MATERIAL_GUARD.capability_artifact_caps(path), MATERIAL_GUARD.CAPABILITY_ARTIFACT_CAPS["research"])



if __name__ == "__main__":
    unittest.main()
