#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "claude_dispatch_headless",
    ROOT / "adapters" / "claude" / "bin" / "dispatch-headless.py",
)
WRAPPER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(WRAPPER)
sys.path.insert(0, str(ROOT / "utilities"))
from model_config import parse_config  # noqa: E402


def selection(**values):
    return SimpleNamespace(
        inherit_model_settings=values.get("inherit", False),
        model_profile=values.get("profile"),
        model_role=values.get("role"),
        model=values.get("model"),
        effort=values.get("effort"),
        registered_worker=values.get("registered_worker", 1),
        dispatch_depth=values.get("dispatch_depth", 1),
        worker_type=values.get("worker_type", "owner"),
        capacity_retry=values.get("capacity_retry", 0),
        route_file=values.get("route_file"),
    )


def top_route(tmp: Path, owner_profile: str = "top") -> str:
    path = tmp / f"route-{owner_profile}.json"
    path.write_text('{"route_id": "rt-top-fixture", "owner_model_profile": "%s", "nodes": []}' % owner_profile, encoding="utf-8")
    return str(path)


SHIPPED_CONF = ROOT / "adapters" / "claude" / "config" / "models.conf"


def shipped_policy() -> dict[str, str]:
    """The shipped file itself (not the user's runtime copy): expectations derive
    from it so the tests follow the config instead of a literal model name."""
    return parse_config(SHIPPED_CONF)


def restricted_policy(*aliases: str) -> dict[str, str]:
    """Shipped policy with an explicit interactive-main-only list — exercises the
    rejection path regardless of what the shipped default declares."""
    return {**shipped_policy(), "CFG_MAIN_SESSION_ONLY_MODELS": " ".join(aliases)}


class ClaudeDispatchModelEligibilityTest(unittest.TestCase):
    def setUp(self):
        # Pin the policy to the shipped file: the user's runtime copy
        # ($CLAUDE_CONFIG_DIR/agent-config/models.conf) must not steer these tests.
        patcher = mock.patch.object(WRAPPER, "_model_policy", side_effect=shipped_policy)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The role mapper (model-map.sh) resolves its harness root from AGENT_HOME:
        # pin it to this checkout so an installed release with another mapping
        # cannot leak into the expected/actual comparison (review B2).
        env = mock.patch.dict(os.environ, {
            "AGENT_HOME": str(ROOT),
            "CLAUDE_CONFIG_DIR": str(ROOT / "adapters" / "claude" / "no-such-runtime-home"),
        })
        env.start()
        self.addCleanup(env.stop)
        for key in ("CLAUDE_MODEL_DEEP", "CLAUDE_EFFORT_DEEP"):
            os.environ.pop(key, None)

    def test_shipped_default_reserves_the_top_model_for_the_main_session(self):
        # 2026-09-09 user rule: shipped default == user runtime mapping. Fable is
        # main-session-only; the deep tier runs the next model down at max effort.
        policy = shipped_policy()
        self.assertEqual(policy["CFG_MAIN_SESSION_ONLY_MODELS"].split(), ["fable"])
        self.assertEqual(policy["CFG_TIER_DEEP_MODEL"], "opus")
        self.assertTrue(WRAPPER._main_session_only_model("claude-fable-5"))
        self.assertFalse(WRAPPER._main_session_only_model(policy["CFG_TIER_DEEP_MODEL"]))

    def test_deep_role_resolves_to_config_deep_tier_and_is_dispatch_eligible(self):
        policy = shipped_policy()
        result = WRAPPER.resolve_model_settings(selection(role="deep orchestrator"))
        # Model and effort are the shipped deep-tier defaults (user-tunable), not literals.
        self.assertEqual(result["model"], policy["CFG_TIER_DEEP_MODEL"])
        self.assertEqual(result["effort"], policy["CFG_TIER_DEEP_EFFORT"])
        self.assertFalse(WRAPPER._main_session_only_model(result["model"]))

    def test_shipped_default_refuses_explicit_fable_headless(self):
        with self.assertRaises(WRAPPER.ModelSelectionError) as refused:
            WRAPPER.resolve_model_settings(selection(model="claude-fable-5", effort="high"))
        self.assertEqual(refused.exception.reason, "headless-main-session-only-model")

    def test_explicit_and_role_override_of_a_main_only_model_are_rejected(self):
        # The rejection path stays live for any config that declares a main-only
        # model. The shipped default now names fable itself, so the second block
        # below pins a different alias: the refusal follows the declared list, not
        # a hardcoded model name.
        with mock.patch.object(WRAPPER, "_model_policy", side_effect=lambda: restricted_policy("fable")):
            with self.assertRaises(WRAPPER.ModelSelectionError) as explicit:
                WRAPPER.resolve_model_settings(selection(model="claude-fable-5", effort="xhigh"))
            self.assertEqual(explicit.exception.reason, "headless-main-session-only-model")
            with mock.patch.dict(os.environ, {"CLAUDE_MODEL_DEEP": "fable"}):
                with self.assertRaises(WRAPPER.ModelSelectionError) as mapped:
                    WRAPPER.resolve_model_settings(selection(role="deep maker"))
            self.assertEqual(mapped.exception.reason, "headless-main-session-only-model")
        with mock.patch.object(WRAPPER, "_model_policy", side_effect=lambda: restricted_policy("sonnet")):
            with self.assertRaises(WRAPPER.ModelSelectionError) as other_alias:
                WRAPPER.resolve_model_settings(selection(model="sonnet", effort="high"))
            self.assertEqual(other_alias.exception.reason, "headless-main-session-only-model")

    def test_inherited_headless_model_is_rejected_before_launch(self):
        with self.assertRaises(WRAPPER.ModelSelectionError) as inherited:
            WRAPPER.resolve_model_settings(selection(inherit=True))
        self.assertEqual(
            inherited.exception.reason,
            "headless-model-inheritance-ineligible",
        )

    def test_missing_main_only_policy_fails_closed(self):
        with mock.patch.object(WRAPPER, "_model_policy", return_value={}):
            with self.assertRaises(WRAPPER.ModelSelectionError) as unavailable:
                WRAPPER.resolve_model_settings(
                    selection(model="sonnet", effort="high")
                )
        self.assertEqual(
            unavailable.exception.reason,
            "dispatch-model-policy-unavailable",
        )

    def test_explicit_eligible_model_remains_explicit(self):
        result = WRAPPER.resolve_model_settings(
            selection(model="sonnet", effort="high")
        )
        self.assertEqual(
            result,
            {
                "source": "explicit",
                "role": "-",
                "profile": "unsealed",
                "tier": "explicit",
                "granularity": "legacy",
                "model": "sonnet",
                "effort": "high",
            },
        )

    def test_legacy_complete_user_copy_keeps_its_main_only_policy(self):
        # Review B1: a user copy seeded from a release before the balanced-deep
        # tier existed must stay selected whole-file after the upgrade instead of
        # falling back to the shipped file.
        #
        # Every rewrite is counted AND the fixture's main-only list is deliberately a
        # value the shipped file does not hold ("fable opus"), so the assertions below
        # can only pass if the user copy actually won. Counting alone was not enough:
        # rewriting a row to the value shipped already has returns count 1 while
        # changing nothing, and the test would then pass identically under a shipped
        # fallback (review MI-3).
        shipped_text = SHIPPED_CONF.read_text(encoding="utf-8")
        legacy, dropped = re.subn(r"^CFG_TIER_BALANCED_DEEP_(MODEL|EFFORT)=.*\n", "", shipped_text, flags=re.MULTILINE)
        self.assertEqual(dropped, 2)
        legacy, retargeted = re.subn(r"^CFG_MODEL_PROFILE_BALANCED_DEEP=.*$", "CFG_MODEL_PROFILE_BALANCED_DEEP=deep:high", legacy, count=1, flags=re.MULTILINE)
        self.assertEqual(retargeted, 1)
        legacy, failover = re.subn(r"^CFG_TIER_DEEP_FAILOVER=.*$", "CFG_TIER_DEEP_FAILOVER=light", legacy, count=1, flags=re.MULTILINE)
        self.assertEqual(failover, 1)
        legacy, main_only = re.subn(r"^CFG_MAIN_SESSION_ONLY_MODELS=.*$", 'CFG_MAIN_SESSION_ONLY_MODELS="fable opus"', legacy, count=1, flags=re.MULTILINE)
        self.assertEqual(main_only, 1)
        self.assertNotIn("CFG_TIER_BALANCED_DEEP_MODEL", legacy)
        # The distinguishing value: shipped restricts fable only, this copy also opus.
        self.assertNotIn("opus", shipped_policy()["CFG_MAIN_SESSION_ONLY_MODELS"].split())
        with tempfile.TemporaryDirectory() as tmp:
            runtime_home = Path(tmp) / "claude-home"
            (runtime_home / "agent-config").mkdir(parents=True)
            (runtime_home / "agent-config" / "models.conf").write_text(legacy, encoding="utf-8")
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(runtime_home)}):
                values, receipt = WRAPPER.resolve_config("claude", source_root=ROOT)
                self.assertEqual((receipt.source, receipt.reason), ("user", "user-valid"))
                self.assertEqual(receipt.unreferenced_tier_keys, "CFG_TIER_BALANCED_DEEP_EFFORT,CFG_TIER_BALANCED_DEEP_MODEL")
                self.assertEqual(values["CFG_MAIN_SESSION_ONLY_MODELS"], "fable opus")
                with mock.patch.object(WRAPPER, "_model_policy", side_effect=lambda: values):
                    self.assertTrue(WRAPPER._main_session_only_model("claude-fable-5"))
                    # opus is refused ONLY because this user copy says so — under the
                    # shipped policy it is the deep tier itself.
                    self.assertTrue(WRAPPER._main_session_only_model("opus"))
                    for alias in ("claude-fable-5", "opus"):
                        with self.assertRaises(WRAPPER.ModelSelectionError) as refused:
                            WRAPPER.resolve_model_settings(selection(model=alias, effort="high"))
                        self.assertEqual(refused.exception.reason, "headless-main-session-only-model")
                self.assertFalse(WRAPPER._main_session_only_model("opus"))  # shipped policy again

    def test_cli_rejects_a_main_only_model_before_registry_prompt_log_or_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            worktree.mkdir()
            subprocess.run(
                ["git", "init", "-q", str(worktree)], check=True
            )
            # A user runtime copy that declares a model the SHIPPED file does not
            # restrict (selected whole-file over the shipped default): rejecting
            # that model proves the CLI reads the user copy, and it must reject
            # before any side effect.
            runtime_home = root / "claude-home"
            (runtime_home / "agent-config").mkdir(parents=True)
            shipped_text = SHIPPED_CONF.read_text(encoding="utf-8")
            restricted_text = re.sub(
                r'^CFG_MAIN_SESSION_ONLY_MODELS=.*$', 'CFG_MAIN_SESSION_ONLY_MODELS="fable sonnet"',
                shipped_text, count=1, flags=re.MULTILINE,
            )
            self.assertNotEqual(restricted_text, shipped_text)
            (runtime_home / "agent-config" / "models.conf").write_text(restricted_text, encoding="utf-8")
            jobs = root / "jobs.log"
            logs = root / "logs"
            env = {key: value for key, value in os.environ.items() if key != "CLAUDE_MODEL_DEEP"}
            env["CLAUDE_CONFIG_DIR"] = str(runtime_home)
            env["AGENT_HOME"] = str(ROOT)
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "adapters" / "claude" / "bin" / "dispatch-headless.py"),
                    "--register",
                    "--worktree", str(worktree),
                    "--jobs", str(jobs),
                    "--log-dir", str(logs),
                    "--slug", "main-only-rejected",
                    "--capability", "autopilot-code",
                    "--capability-mode", "dev",
                    "--qa", "standard",
                    "--model", "sonnet",
                    "--effort", "xhigh",
                    "--prompt-text", "must not launch",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env=env,
            )
            self.assertEqual(result.returncode, 64, result.stdout + result.stderr)
            self.assertIn("reason=headless-main-session-only-model", result.stdout)
            self.assertIn("child_spawned=0", result.stdout)
            self.assertFalse(jobs.exists())
            self.assertFalse(logs.exists())



class TopExceptionProfileTest(ClaudeDispatchModelEligibilityTest):
    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp())
        self.route = top_route(self.tmp)

    def test_the_sealed_top_profile_is_the_one_door_to_the_main_only_model(self):
        policy = shipped_policy()
        result = WRAPPER.resolve_model_settings(selection(profile="top", route_file=self.route))
        self.assertEqual((result["model"], result["effort"], result["source"], result["tier"]),
                         (policy["CFG_TIER_TOP_MODEL"], policy["CFG_TIER_TOP_EFFORT"], "profile-top", "top"))
        self.assertTrue(WRAPPER._main_session_only_model(result["model"]))

    def test_top_requires_the_route_that_sealed_it(self):
        # top review B1: a route-less depth-1 owner resolved fable/max with no
        # demand recorded anywhere; the wrapper now requires the sealing route.
        with self.assertRaises(WRAPPER.ModelSelectionError) as refused:
            WRAPPER.resolve_model_settings(selection(profile="top"))
        self.assertEqual(refused.exception.reason, "profile-top-route-required")
        with self.assertRaises(WRAPPER.ModelSelectionError) as mismatch:
            WRAPPER.resolve_model_settings(selection(profile="top", route_file=top_route(self.tmp, "balanced-deep")))
        self.assertEqual(mismatch.exception.reason, "profile-top-route-mismatch")
        # the owner route binding (standard+) counts as the route too
        binding = SimpleNamespace(route_file=self.route)
        args = selection(profile="top"); args.owner_route_binding = binding
        self.assertEqual(WRAPPER.resolve_model_settings(args)["source"], "profile-top")
        # other profiles need no route
        self.assertEqual(WRAPPER.resolve_model_settings(selection(profile="deep"))["source"], "profile")

    def test_a_frame_anchor_is_admitted_by_its_own_node_seal(self):
        # THE THREE-HARNESS PARITY POINT. The route seals `top` on the frame
        # anchor while the owner stays `deep`, so this wrapper has to check the
        # LAUNCHING NODE's seal, not the owner's. Each harness keeps its own
        # copy of that call; a harness that forgets to pass `--route-node`
        # through refuses a correctly compiled frame anchor, and only that one
        # harness does -- which is exactly how a parity gap hides. This test
        # exists in all three eligibility suites on purpose.
        import json as _json, tempfile as _tempfile
        _tmp = Path(_tempfile.mkdtemp())
        path = _tmp / "frame-route.json"
        path.write_text(_json.dumps({
            "route_id": "rt-frame-fixture",
            "owner_model_profile": "deep",
            "nodes": [{"id": "frame", "model_profile": "top"},
                      {"id": "frame-alternative", "model_profile": "deep"}],
        }), encoding="utf-8")

        def frame_args(node):
            # A non-owner worker must also carry its independently sealed
            # role; `plan/frame`'s role is `deep maker`.
            args = selection(profile="top", route_file=str(path),
                             worker_type="frame", role="deep maker")
            args.route_node = node
            return args

        self.assertEqual(
            WRAPPER.resolve_model_settings(frame_args("frame"))["profile"], "top")
        # No node -> the owner's `deep` seal, and the owner door stays shut.
        with self.assertRaises(WRAPPER.ModelSelectionError) as owner_view:
            WRAPPER.resolve_model_settings(frame_args(None))
        self.assertEqual(owner_view.exception.reason, "profile-top-route-mismatch")
        # The sibling leg sealed `deep`, so its own seal refuses it.
        with self.assertRaises(WRAPPER.ModelSelectionError) as sibling:
            WRAPPER.resolve_model_settings(frame_args("frame-alternative"))
        self.assertEqual(sibling.exception.reason, "profile-top-route-mismatch")
        # A node the route never declared is named as a wiring bug.
        with self.assertRaises(WRAPPER.ModelSelectionError) as unknown:
            WRAPPER.resolve_model_settings(frame_args("frame-contrarian"))
        self.assertEqual(unknown.exception.reason, "profile-top-route-node-unknown")

    def test_no_model_override_runs_under_the_top_label(self):
        # top review m1: no cascade in or out, capacity retry included
        for model, effort in (("claude-fable-5", "max"), ("opus", "xhigh")):
            with self.subTest(model=model), self.assertRaises(WRAPPER.ModelSelectionError) as refused:
                WRAPPER.resolve_model_settings(selection(profile="top", route_file=self.route, model=model, effort=effort, capacity_retry=1))
            self.assertEqual(refused.exception.reason, "profile-top-override-forbidden")

    def test_top_is_refused_below_dispatch_depth_one_and_for_review_workers(self):
        for kwargs in (dict(dispatch_depth=2, worker_type="stage"), dict(dispatch_depth=2, worker_type="review"),
                       dict(worker_type="support"), dict(worker_type="review")):
            with self.subTest(**kwargs), self.assertRaises(WRAPPER.ModelSelectionError) as refused:
                WRAPPER.resolve_model_settings(selection(profile="top", route_file=self.route, **kwargs))
            self.assertEqual(refused.exception.reason, "invalid-dispatch-model-profile")

    def test_explicit_and_role_selection_of_the_top_model_stay_refused(self):
        with self.assertRaises(WRAPPER.ModelSelectionError) as explicit:
            WRAPPER.resolve_model_settings(selection(model="fable", effort="max"))
        self.assertEqual(explicit.exception.reason, "headless-main-session-only-model")
        with mock.patch.dict(os.environ, {"CLAUDE_MODEL_DEEP": "fable"}):
            with self.assertRaises(WRAPPER.ModelSelectionError) as mapped:
                WRAPPER.resolve_model_settings(selection(role="deep maker"))
        self.assertEqual(mapped.exception.reason, "headless-main-session-only-model")

if __name__ == "__main__":
    unittest.main()
