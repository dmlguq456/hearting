#!/usr/bin/env python3
"""Codex parity for the main-session-only gate and the `top` exception profile (2026-09-10)."""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import os
from pathlib import Path
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "codex_dispatch_headless", ROOT / "adapters" / "codex" / "bin" / "dispatch-headless.py")
WRAPPER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(WRAPPER)
sys.path.insert(0, str(ROOT / "utilities"))
from model_config import parse_config  # noqa: E402

SHIPPED_CONF = ROOT / "adapters" / "codex" / "config" / "models.conf"


def selection(**values):
    return SimpleNamespace(
        inherit_model_settings=values.get("inherit", False),
        model_profile=values.get("profile"),
        model_role=values.get("role"),
        model=values.get("model"),
        reasoning=values.get("reasoning"),
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


def shipped_policy() -> dict[str, str]:
    return parse_config(SHIPPED_CONF)


def stage_top_route(tmp: Path, name: str = "stage-top", **over) -> str:
    """A strong route that sealed `top` on the stage node `inspect` and on the review leg
    `review-alternative`, each named by the person in `explicit_profiles`; the owner stays `deep`."""
    import json as _json
    route = {"route_id": "rt-stage-top-fixture", "owner_model_profile": "deep", "effective_intensity": "strong",
             "explicit_profiles": {"inspect": "top", "review-alternative": "top"},
             "nodes": [{"id": "inspect", "dispatch_depth": 2, "model_profile": "top"},
                       {"id": "report", "dispatch_depth": 2, "model_profile": "deep"},
                       {"id": "review", "dispatch_depth": 2, "model_profile": "deep"},
                       {"id": "review-alternative", "dispatch_depth": 2, "model_profile": "top"}]}
    route.update(over)
    path = tmp / f"{name}.json"
    path.write_text(_json.dumps(route), encoding="utf-8")
    return str(path)


class CodexDispatchModelEligibilityTest(unittest.TestCase):
    def setUp(self):
        self.runtime_home = tempfile.TemporaryDirectory(prefix="hearting-codex-model-home-")
        self.addCleanup(self.runtime_home.cleanup)
        self.tmp = Path(self.runtime_home.name)
        patcher = mock.patch.object(WRAPPER, "_model_policy", side_effect=shipped_policy)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {
            "AGENT_HOME": str(ROOT),
            "HOME": self.runtime_home.name,
            "CODEX_HOME": str(ROOT / "adapters" / "codex" / "no-such-runtime-home"),
            "HEARTING_MODEL_CONFIG_HOME": "",
        })
        env.start()
        self.addCleanup(env.stop)

    def test_shipped_default_declares_its_main_session_only_models(self):
        policy = shipped_policy()
        restricted = policy["CFG_MAIN_SESSION_ONLY_MODELS"].split()
        self.assertTrue(restricted)
        for model in restricted:
            self.assertTrue(WRAPPER._main_session_only_model(model))
        self.assertFalse(WRAPPER._main_session_only_model(policy["CFG_TIER_DEEP_MODEL"]))

    def test_explicit_main_only_model_is_refused_and_an_eligible_model_passes(self):
        restricted = shipped_policy()["CFG_MAIN_SESSION_ONLY_MODELS"].split()
        self.assertTrue(restricted)
        with self.assertRaises(WRAPPER.ModelSelectionError) as refused:
            WRAPPER.resolve_model_settings(selection(model=restricted[0], reasoning="xhigh"))
        self.assertEqual(refused.exception.reason, "headless-main-session-only-model")
        result = WRAPPER.resolve_model_settings(selection(model="gpt-5.6-sol", reasoning="high"))
        self.assertEqual((result["source"], result["model"]), ("explicit", "gpt-5.6-sol"))

    def test_a_role_mapped_to_a_main_only_model_is_refused(self):
        restricted = shipped_policy()["CFG_MAIN_SESSION_ONLY_MODELS"].split()
        self.assertTrue(restricted)
        with mock.patch.object(WRAPPER, "role_map", return_value={"exact_model_id": restricted[0], "reasoning": "high"}):
            with self.assertRaises(WRAPPER.ModelSelectionError) as refused:
                WRAPPER.resolve_model_settings(selection(role="deep maker"))
        self.assertEqual(refused.exception.reason, "headless-main-session-only-model")

    def test_the_sealed_top_profile_is_the_configured_exception(self):
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        route = top_route(tmp)
        policy = shipped_policy()
        result = WRAPPER.resolve_model_settings(selection(profile="top", route_file=route))
        self.assertEqual((result["model"], result["reasoning"], result["source"], result["tier"]),
                         (policy["CFG_TIER_TOP_MODEL"], policy["CFG_TIER_TOP_EFFORT"], "profile-top", "top"))
        self.assertTrue(WRAPPER._main_session_only_model(policy["CFG_TIER_TOP_MODEL"]))
        with self.assertRaises(WRAPPER.ModelSelectionError) as no_route:
            WRAPPER.resolve_model_settings(selection(profile="top"))
        self.assertEqual(no_route.exception.reason, "profile-top-route-required")
        with self.assertRaises(WRAPPER.ModelSelectionError) as override:
            WRAPPER.resolve_model_settings(selection(profile="top", route_file=route, model="gpt-5.6-sol", reasoning="xhigh", capacity_retry=1))
        self.assertEqual(override.exception.reason, "profile-top-override-forbidden")
        with self.assertRaises(WRAPPER.ModelSelectionError) as depth:
            WRAPPER.resolve_model_settings(selection(profile="top", route_file=route, dispatch_depth=2, worker_type="stage"))
        # a depth-2 stage is admitted only by its own node's named seal, not the owner's
        self.assertEqual(depth.exception.reason, "profile-top-route-mismatch")
        with self.assertRaises(WRAPPER.ModelSelectionError) as support:
            WRAPPER.resolve_model_settings(selection(profile="top", route_file=route, worker_type="support"))
        self.assertEqual(support.exception.reason, "invalid-dispatch-model-profile")

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

    def test_no_headless_launch_may_inherit_the_interactive_model(self):
        # top review M1 + combined review B1: this machine's interactive
        # default is Astra, and no headless launch -- registered or not --
        # can prove otherwise, so inheritance is refused outright (parity
        # with the Claude adapter, which never accepted it).
        for registered in (1, 0):
            with self.subTest(registered_worker=registered), \
                 self.assertRaises(WRAPPER.ModelSelectionError) as refused:
                WRAPPER.resolve_model_settings(selection(inherit=True, registered_worker=registered))
            self.assertEqual(refused.exception.reason, "headless-model-inheritance-ineligible")
        # the exclusivity check still comes first
        with self.assertRaises(WRAPPER.ModelSelectionError) as combined:
            WRAPPER.resolve_model_settings(selection(inherit=True, profile="deep"))
        self.assertEqual(combined.exception.reason, "invalid-dispatch-model-selection")

    def test_a_user_copy_without_the_key_carries_no_restriction(self):
        without = {k: v for k, v in shipped_policy().items() if k != "CFG_MAIN_SESSION_ONLY_MODELS"}
        with mock.patch.object(WRAPPER, "_model_policy", return_value=without):
            restricted = shipped_policy()["CFG_MAIN_SESSION_ONLY_MODELS"].split()
            self.assertTrue(restricted)
            self.assertFalse(WRAPPER._main_session_only_model(restricted[0]))

    def test_the_receipt_reports_whether_the_key_is_declared(self):
        self.assertEqual(WRAPPER._main_session_only_policy_state(), "declared")
        without = {k: v for k, v in shipped_policy().items() if k != "CFG_MAIN_SESSION_ONLY_MODELS"}
        with mock.patch.object(WRAPPER, "_model_policy", return_value=without):
            self.assertEqual(WRAPPER._main_session_only_policy_state(), "absent")
        with mock.patch.object(WRAPPER, "_model_policy",
                               side_effect=WRAPPER.ModelSelectionError("dispatch-model-policy-unavailable", "x")):
            self.assertEqual(WRAPPER._main_session_only_policy_state(), "unavailable")

    def test_deep_profile_and_ordinary_roles_are_unaffected(self):
        result = WRAPPER.resolve_model_settings(selection(profile="deep"))
        self.assertEqual((result["source"], result["model"]), ("profile", shipped_policy()["CFG_TIER_DEEP_MODEL"]))

    def _stage_args(self, route, node, worker_type="stage", **over):
        args = selection(profile="top", route_file=route, dispatch_depth=2, worker_type=worker_type,
                         role="deep reviewer", **over)
        args.route_node = node
        return args

    def test_a_person_named_top_stage_or_review_node_gets_the_top_budget(self):
        # 2026-11-23: the stage worker's launch carries the top budget of this adapter's own config,
        # exactly what the owner's top selection resolves to; nothing is called, nothing is installed.
        policy = parse_config(SHIPPED_CONF)
        owner = WRAPPER.resolve_model_settings(selection(profile="top", route_file=top_route(self.tmp)))
        for worker_type, node in (("stage", "inspect"), ("review", "review-alternative")):
            with self.subTest(worker_type=worker_type, node=node):
                result = WRAPPER.resolve_model_settings(
                    self._stage_args(stage_top_route(self.tmp), node, worker_type))
                self.assertEqual((result["model"], result["reasoning"], result["profile"], result["tier"]),
                                 (owner["model"], owner["reasoning"], "top", owner["tier"]))
                self.assertEqual(result["source"], owner["source"])
                self.assertEqual(result["role"], "deep reviewer")
                self.assertTrue(WRAPPER._main_session_only_model(result["model"]))

    def test_an_unsealed_top_stage_selection_is_refused(self):
        # a hand-given `--model-profile top`, a route that never named the node, another node, a lower
        # intensity, an old route that sealed top for the owner only, and a stage launch with no node
        base = dict(owner_model_profile="deep")
        cases = {
            "no route": (None, "inspect", "profile-top-route-required"),
            "owner-only route": (top_route(self.tmp), "inspect", "profile-top-route-mismatch"),
            "other node": (stage_top_route(self.tmp), "report", "profile-top-route-mismatch"),
            "unknown node": (stage_top_route(self.tmp), "ghost", "profile-top-route-mismatch"),
            "no node": (stage_top_route(self.tmp), None, "profile-top-route-mismatch"),
            "standard route": (stage_top_route(self.tmp, "std", effective_intensity="standard"), "inspect",
                               "profile-top-route-mismatch"),
            "not named": (stage_top_route(self.tmp, "unnamed", explicit_profiles={}), "inspect",
                          "profile-top-route-mismatch"),
        }
        for label, (route, node, reason) in cases.items():
            with self.subTest(label), self.assertRaises(WRAPPER.ModelSelectionError) as refused:
                WRAPPER.resolve_model_settings(self._stage_args(route, node))
            self.assertEqual(refused.exception.reason, reason)

    def test_a_named_top_stage_node_admits_no_model_override_and_an_unregistered_worker_is_refused(self):
        with self.assertRaises(WRAPPER.ModelSelectionError) as override:
            WRAPPER.resolve_model_settings(self._stage_args(
                stage_top_route(self.tmp), "inspect", model="some-model", reasoning="low", capacity_retry=1))
        self.assertEqual(override.exception.reason, "profile-top-override-forbidden")
        with self.assertRaises(WRAPPER.ModelSelectionError) as unregistered:
            WRAPPER.resolve_model_settings(self._stage_args(
                stage_top_route(self.tmp), "inspect", registered_worker=0))
        self.assertEqual(unregistered.exception.reason, "invalid-dispatch-model-profile")


if __name__ == "__main__":
    unittest.main()
