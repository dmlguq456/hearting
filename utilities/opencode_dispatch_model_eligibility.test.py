#!/usr/bin/env python3
"""opencode's share of the `top` exception profile (guard review m2, 2026-09-10).

The claude and codex wrappers each have a suite pinning `top`; opencode's
branch shipped without one, and it is the branch that differs -- `top`
collapses onto the deep tier here, so the refusal reads the *requested*
profile rather than the resolved one. This pins that behaviour, and that
inheritance stays open while the shipped config declares no main-session-only
model (`model_config.inheritance_refusal`, the same rule on every adapter).
"""
from __future__ import annotations
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "opencode_dispatch_headless", ROOT / "adapters" / "opencode" / "bin" / "dispatch-headless.py")
WRAPPER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(WRAPPER)
sys.path.insert(0, str(ROOT / "utilities"))
from model_config import parse_config  # noqa: E402

SHIPPED_CONF = ROOT / "adapters" / "opencode" / "config" / "models.conf"


def selection(**values):
    return SimpleNamespace(
        inherit_model_settings=values.get("inherit", False),
        model_profile=values.get("profile"),
        model_role=values.get("role"),
        model=values.get("model"),
        variant=values.get("variant"),
        registered_worker=values.get("registered_worker", 1),
        dispatch_depth=values.get("dispatch_depth", 1),
        worker_type=values.get("worker_type", "owner"),
        capacity_retry=values.get("capacity_retry", 0),
        route_file=values.get("route_file"),
    )


class OpencodeDispatchModelEligibilityTest(unittest.TestCase):
    def setUp(self):
        # resolve against the shipped file, not whichever copy this machine
        # carries, so the assertions below describe the release contract
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        env = mock.patch.dict(os.environ, {
            "AGENT_HOME": str(ROOT),
            "XDG_CONFIG_HOME": str(self.tmp / "no-such-runtime-config"),
            "XDG_DATA_HOME": str(self.tmp / "no-such-runtime-data"),
            "OPENCODE_RUNTIME_VERSION": "1.19.0",
        })
        env.start()
        self.addCleanup(env.stop)
        store = mock.patch.object(WRAPPER.opencode_runtime, "credential_store_present",
                                  return_value=False)
        store.start()
        self.addCleanup(store.stop)

    def top_route(self) -> str:
        path = self.tmp / "route-top.json"
        path.write_text(
            '{"route_id": "rt-top-fixture", "owner_model_profile": "top", "nodes": []}',
            encoding="utf-8")
        return str(path)

    def test_top_collapses_onto_the_balanced_deep_tier_and_admits_no_override(self):
        policy = parse_config(SHIPPED_CONF)
        route = self.top_route()
        result = WRAPPER.resolve_model_settings(selection(profile="top", route_file=route))
        # the label survives on the receipt; the tier does not exist here
        self.assertEqual(
            (result["profile"], result["tier"], result["granularity"]),
            ("top", "balanced-deep", "collapsed-top-to-balanced-deep"))
        self.assertEqual(result["model"], policy["CFG_TIER_BALANCED_DEEP_MODEL"])
        self.assertNotIn("CFG_TIER_TOP_MODEL", policy)
        # the collapse is exactly why the check reads args.model_profile: the
        # resolved profile is still "top" here, but a future adapter that
        # rewrote the label on collapse would slip past a resolved-value check
        with self.assertRaises(WRAPPER.ModelSelectionError) as override:
            WRAPPER.resolve_model_settings(selection(
                profile="top", route_file=route, model=result["model"],
                variant="high", capacity_retry=1))
        self.assertEqual(override.exception.reason, "profile-top-override-forbidden")

    def test_top_still_needs_its_sealing_route_and_a_depth_1_owner(self):
        route = self.top_route()
        with self.assertRaises(WRAPPER.ModelSelectionError) as no_route:
            WRAPPER.resolve_model_settings(selection(profile="top"))
        self.assertEqual(no_route.exception.reason, "profile-top-route-required")
        with self.assertRaises(WRAPPER.ModelSelectionError) as depth:
            WRAPPER.resolve_model_settings(selection(
                profile="top", route_file=route, dispatch_depth=2,
                worker_type="stage", role="_kernel/owner"))
        self.assertEqual(depth.exception.reason, "invalid-dispatch-model-profile")

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

    def test_inheritance_stays_allowed_because_nothing_here_is_main_only(self):
        # the shipped config declares no main-session-only list, so an
        # inherited model can leak nothing (`headless_model_policy.test.py`
        # covers the declared case on all three adapters).
        result = WRAPPER.resolve_model_settings(selection(inherit=True))
        self.assertEqual(result["source"], "inherit")
        with self.assertRaises(WRAPPER.ModelSelectionError) as combined:
            WRAPPER.resolve_model_settings(selection(inherit=True, profile="deep"))
        self.assertEqual(combined.exception.reason, "invalid-dispatch-model-selection")

    def test_v2_automatic_profile_uses_only_configured_unavailable_fallbacks(self):
        with mock.patch.object(WRAPPER.opencode_runtime, "is_v2", return_value=True), \
             mock.patch.object(WRAPPER.opencode_runtime, "select_automatic_model",
                               return_value=("openai/gpt-6.1-sol", "runtime-default",
                                             {"state": "unknown", "reason": "no-observation"})) as select:
            result = WRAPPER.resolve_model_settings(selection(profile="balanced-deep", role="deep maker"))
        select.assert_called_once()
        self.assertEqual(result["model"], "openai/gpt-6.1-sol")
        self.assertEqual(result["variant"], "runtime-default")
        self.assertEqual(result["source"], "profile+provider-fallback")
        self.assertNotIn(("opencode/beam-free", "runtime-default"),
                         WRAPPER.opencode_runtime.automatic_model_fallbacks())

    def test_v2_profile_launch_keeps_an_unverified_primary_and_refuses_only_a_rejection(self):
        # Automatic placement has already left an unverified OpenCode out, so a
        # profile launch that still arrives here was chosen explicitly and runs
        # the configured model; only a confirmed rejection is refused.
        args = selection(profile="balanced-deep", role="deep maker")
        unverified = {"state": "unknown", "reason": "provider-model-unverified",
                      "automatic_eligible": False}
        with mock.patch.object(WRAPPER.opencode_runtime, "is_v2", return_value=True), \
             mock.patch.object(WRAPPER.opencode_runtime, "select_automatic_model",
                               side_effect=lambda model, variant, **kwargs: (model, variant, unverified)):
            kept = WRAPPER.resolve_model_settings(args)
        self.assertEqual(kept["source"], "profile")
        self.assertNotIn("provider_fallback_from", kept)
        rejected = {"state": "unavailable", "reason": "authentication-rejected",
                    "automatic_eligible": False}
        with mock.patch.object(WRAPPER.opencode_runtime, "is_v2", return_value=True), \
             mock.patch.object(WRAPPER.opencode_runtime, "select_automatic_model",
                               side_effect=lambda model, variant, **kwargs: (model, variant, rejected)), \
             self.assertRaises(WRAPPER.ModelSelectionError) as refused:
            WRAPPER.resolve_model_settings(args)
        self.assertEqual(refused.exception.reason, "opencode-model-unavailable")
        moved = {"state": "unknown", "automatic_eligible": True,
                 "reason": "no-observation", "fallback_from_reason": "provider-model-unverified"}
        with mock.patch.object(WRAPPER.opencode_runtime, "is_v2", return_value=True), \
             mock.patch.object(WRAPPER.opencode_runtime, "select_automatic_model",
                               return_value=("opencode-go/fallback", "max", moved)):
            fallback = WRAPPER.resolve_model_settings(args)
        self.assertEqual((fallback["model"], fallback["provider_fallback_reason"]),
                         ("opencode-go/fallback", "provider-model-unverified"))

    def test_provider_fallback_provenance_reaches_the_launch_receipt(self):
        # The receipt already names the final model and the synthesized
        # model_source; these extra fields say which provider was replaced and
        # why, and stay absent when the launch kept its first choice.
        args = selection(profile="balanced-deep", role="deep maker")
        with mock.patch.object(WRAPPER.opencode_runtime, "is_v2", return_value=False):
            primary = WRAPPER.resolve_model_settings(args)
        self.assertEqual(WRAPPER.provider_fallback_fields(primary), {})
        moved = {"state": "unknown", "automatic_eligible": True,
                 "reason": "no-observation", "fallback_from_reason": "provider-model-unverified"}
        with mock.patch.object(WRAPPER.opencode_runtime, "is_v2", return_value=True), \
             mock.patch.object(WRAPPER.opencode_runtime, "select_automatic_model",
                               return_value=("opencode-go/fallback", "max", moved)):
            fallback = WRAPPER.resolve_model_settings(args)
        self.assertEqual(
            WRAPPER.provider_fallback_fields(fallback),
            {"provider_fallback_from": primary["model"],
             "provider_fallback_reason": "provider-model-unverified"})

    def test_explicit_model_remains_exact_even_when_provider_cache_rejects_it(self):
        with mock.patch.object(WRAPPER.opencode_runtime, "is_v2", return_value=True), \
             mock.patch.object(WRAPPER.opencode_runtime, "select_automatic_model") as select:
            result = WRAPPER.resolve_model_settings(selection(
                model="opencode-go/muse-spark-1.3-contributor", variant="xhigh"))
        select.assert_not_called()
        self.assertEqual(result["model"], "opencode-go/muse-spark-1.3-contributor")
        self.assertEqual(result["source"], "explicit")


if __name__ == "__main__":
    unittest.main()
