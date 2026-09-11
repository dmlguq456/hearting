#!/usr/bin/env python3
"""opencode's share of the `top` exception profile (guard review m2, 2026-09-10).

The claude and codex wrappers each have a suite pinning `top`; opencode's
branch shipped without one, and it is the branch that differs -- `top`
collapses onto the deep tier here, so the refusal reads the *requested*
profile rather than the resolved one. This pins that behaviour and the one
documented asymmetry (opencode may still inherit the interactive model).
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
        })
        env.start()
        self.addCleanup(env.stop)

    def top_route(self) -> str:
        path = self.tmp / "route-top.json"
        path.write_text(
            '{"route_id": "rt-top-fixture", "owner_model_profile": "top", "nodes": []}',
            encoding="utf-8")
        return str(path)

    def test_top_collapses_onto_the_deep_tier_and_admits_no_override(self):
        policy = parse_config(SHIPPED_CONF)
        route = self.top_route()
        result = WRAPPER.resolve_model_settings(selection(profile="top", route_file=route))
        # the label survives on the receipt; the tier does not exist here
        self.assertEqual(
            (result["profile"], result["tier"], result["granularity"]),
            ("top", "deep", "collapsed-top-to-deep"))
        self.assertEqual(result["model"], policy["CFG_TIER_DEEP_MODEL"])
        self.assertNotIn("CFG_TIER_TOP_MODEL", policy)
        # the collapse is exactly why the check reads args.model_profile: the
        # resolved profile is still "top" here, but a future adapter that
        # rewrote the label on collapse would slip past a resolved-value check
        with self.assertRaises(WRAPPER.ModelSelectionError) as override:
            WRAPPER.resolve_model_settings(selection(
                profile="top", route_file=route, model="opencode-go/qwen3.8-max",
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
        # the asymmetry claude and codex refuse: this adapter declares no
        # main-session-only list, so an inherited model can leak nothing.
        # `model_config.test.py::test_opencode_declares_no_main_session_only_policy`
        # reddens the day that stops being true.
        result = WRAPPER.resolve_model_settings(selection(inherit=True))
        self.assertEqual(result["source"], "inherit")
        with self.assertRaises(WRAPPER.ModelSelectionError) as combined:
            WRAPPER.resolve_model_settings(selection(inherit=True, profile="deep"))
        self.assertEqual(combined.exception.reason, "invalid-dispatch-model-selection")


if __name__ == "__main__":
    unittest.main()
